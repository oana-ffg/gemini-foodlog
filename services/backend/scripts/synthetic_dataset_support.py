from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

from PIL import Image

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.grouping import CaptureGroupingService
from foodlog_backend.inference_schema import (
    ActivityMealInferenceV1,
    DirectObservation,
    ImageEvidenceLink,
    InferenceConfidence,
    InferenceKind,
    InferenceMealComponent,
    UserAction,
)
from foodlog_backend.models import (
    Account,
    ActivityEvent,
    BrowserCamera,
    CaptureEnvelopeV1,
    CaptureRecord,
    CaptureStatus,
    Confidence,
    MealEntry,
    event_inference_job_id,
    utc_now,
)
from foodlog_backend.storage import GCSObjectStore


async def publish_synthetic_event(
    repository: FirestoreRepository,
    *,
    event: ActivityEvent,
    captures: list[CaptureRecord],
    title: str,
    evidence_description: str,
    rationale: str,
    lease_owner: str,
) -> MealEntry:
    """Publish a labelled no-model fixture event through normal revision semantics."""
    lease_id = str(uuid4())
    claimed = await repository.claim_job(
        account_id=event.account_id,
        job_id=event_inference_job_id(event.id),
        expected_subject_revision=event.current_revision,
        lease_id=lease_id,
        lease_owner=lease_owner,
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    if claimed is None:
        raise RuntimeError("synthetic event inference job could not be claimed")
    published = await repository.publish_event_inference(
        account_id=event.account_id,
        event_id=event.id,
        expected_event_revision=event.current_revision,
        lease_id=lease_id,
        lease_owner=lease_owner,
        hypothesis=ActivityMealInferenceV1(
            schema_version="activity-meal-inference-v1",
            event_id=event.id,
            source_capture_ids=[item.id for item in captures],
            kind=InferenceKind.TENTATIVE_MEAL,
            best_guess=title,
            confidence=InferenceConfidence.LIKELY,
            components=[
                InferenceMealComponent(
                    id="synthetic_meal",
                    name=title,
                    ingredients=[title],
                    preparation_methods=[],
                    confidence=InferenceConfidence.LIKELY,
                    alternatives=[],
                    evidence_ids=["synthetic_observation"],
                )
            ],
            direct_observations=[
                DirectObservation(
                    id="synthetic_observation",
                    description=evidence_description,
                    image_evidence=[
                        ImageEvidenceLink(capture_id=item.id) for item in captures
                    ],
                )
            ],
            contextual_evidence=[],
            assumptions=[],
            deductions=[],
            alternatives=[],
            rationale=rationale,
            allowed_actions=[
                UserAction.CONFIRM_GUESS,
                UserAction.CORRECT,
                UserAction.DISCARD_NOT_COOKING,
            ],
        ),
    )
    if published is None:
        raise RuntimeError("synthetic event inference was not published")
    return published


async def seed_synthetic_meal(
    repository: FirestoreRepository,
    store: GCSObjectStore,
    grouping: CaptureGroupingService,
    *,
    account: Account,
    camera: BrowserCamera,
    image: bytes,
    local_at: datetime,
    title: str,
    sequence_id: str,
    sequence_number: int,
    idempotency_key: str,
    client_version: str,
    worker_id: str,
    lease_owner: str,
    evidence_description: str,
    rationale: str,
    capture_id: str | None = None,
) -> MealEntry:
    """Persist one explicitly synthetic historical meal without a model call.

    A caller may supply a deterministic capture ID to make a release-dataset seed
    safely resumable. Exact retries reuse the already stored object and published
    meal; changed bytes or metadata still fail through the repository's normal
    idempotency contract.
    """
    capture_id = capture_id or str(uuid4())
    digest = sha256(image).hexdigest()
    object_key = f"accounts/{account.id}/captures/{capture_id}.png"
    captured_at = local_at.astimezone(UTC)
    with Image.open(BytesIO(image)) as decoded:
        width, height = decoded.size
    metadata = CaptureEnvelopeV1(
        camera_id=camera.id,
        captured_at=local_at,
        client_kind="browser",
        client_version=client_version,
        sequence_id=sequence_id,
        sequence_number=sequence_number,
        width=width,
        height=height,
    )
    capture, _, created = await repository.reserve_capture(
        capture_id=capture_id,
        account=account,
        camera=camera,
        idempotency_key=idempotency_key,
        content_type="image/png",
        content_sha256=digest,
        object_key=object_key,
        metadata=metadata,
    )
    if created:
        if not await store.put(account.id, object_key, image, "image/png"):
            raise RuntimeError("new synthetic capture object already exists")
    else:
        if not await store.put(account.id, object_key, image, "image/png"):
            retained = await store.get(account.id, object_key)
            if sha256(retained).hexdigest() != digest:
                raise RuntimeError("retained synthetic capture object changed")
    if capture.status == CaptureStatus.ACCEPTED:
        await repository.mark_stored(account_id=account.id, capture_id=capture.id)
    grouped = await grouping.process(
        account_id=account.id,
        capture_id=capture.id,
        worker_id=worker_id,
    )
    if grouped is None:
        capture = await repository.capture_for_account(
            account_id=account.id,
            capture_id=capture.id,
        )
        if capture.event_id is None:
            raise RuntimeError("synthetic capture was not grouped")
        event, captures = await repository.event_evidence_for_account(
            account_id=account.id,
            event_id=capture.event_id,
        )
    else:
        event, captures = await repository.event_evidence_for_account(
            account_id=account.id,
            event_id=grouped.event.id,
        )
    if event.meal_id is not None:
        published = await repository.meal_for_owner(account.owner_user_id, event.meal_id)
        if published.title != title:
            raise RuntimeError("retained synthetic meal changed")
        return published
    published = await publish_synthetic_event(
        repository,
        event=event,
        captures=captures,
        title=title,
        evidence_description=evidence_description,
        rationale=rationale,
        lease_owner=lease_owner,
    )
    if published.title != title or published.confidence != Confidence.LIKELY:
        raise RuntimeError("synthetic meal publication changed the labelled result")
    if published.occurred_at != captured_at:
        raise RuntimeError("synthetic meal publication changed the event time")
    return published
