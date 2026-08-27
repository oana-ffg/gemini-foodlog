from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.async_client import AsyncClient

from .feedback_learning import FeedbackLearningService
from .firestore_repository import FirestoreRepository
from .inference_schema import ActivityMealInferenceV1
from .knowledge_updates import (
    ConfirmedClaimSource,
    HouseholdKnowledgeUpdater,
    KnowledgeUpdateIntent,
    KnowledgeUpdateProposal,
)
from .models import (
    ActivityEvent,
    CaptureRecord,
    CaptureStatus,
    Confidence,
    DurableJob,
    JobKind,
    KnowledgeBeliefStrength,
    KnowledgeClaim,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeLifecycle,
    KnowledgeRevisionDraft,
    KnowledgeRevisionSource,
    MealComponent,
    MealEntry,
    MealFeedbackKind,
    MealFeedbackLearningDisposition,
    MealFeedbackRequest,
    QuestionAnswerRequest,
    QuestionStatus,
    event_inference_job_id,
    utc_now,
)
from .repository import Repository

SMOKE_ACCOUNT_ID = "repository-smoke-account-v1"
SMOKE_OWNER_ID = "repository-smoke-owner-v1"
SMOKE_CAPTURE_ID = "repository-smoke-capture-v1"
SMOKE_MEAL_ID = "repository-smoke-meal-v1"
SMOKE_FIXTURE_VERSION = "firestore-repository-smoke-v1"
SMOKE_EVENT_ID = "repository-smoke-event-publication-v1"
SMOKE_EVENT_CAPTURE_ID = "repository-smoke-event-capture-v1"
SMOKE_EVENT_CAMERA_ID = "repository-smoke-event-camera-v1"
SMOKE_EVENT_LEASE_ID = "repository-smoke-event-lease-v1"
SMOKE_EVENT_WORKER_ID = "repository-smoke-event-worker-v1"


async def _ensure_document(reference, expected: dict[str, Any]) -> None:
    snapshot = await reference.get()
    if not snapshot.exists:
        try:
            await reference.create(expected)
            return
        except AlreadyExists:
            snapshot = await reference.get()
    data = snapshot.to_dict() or {}
    for field, value in expected.items():
        if field in {"available_at", "created_at", "updated_at"}:
            continue
        if data.get(field) != value:
            raise RuntimeError(f"Smoke fixture collision at {reference.path}: {field}")


async def ensure_smoke_fixture(client: AsyncClient) -> None:
    created_at = utc_now()
    account_ref = client.collection("accounts").document(SMOKE_ACCOUNT_ID)
    await _ensure_document(
        account_ref,
        {
            "schema_version": 1,
            "id": SMOKE_ACCOUNT_ID,
            "owner_user_id": SMOKE_OWNER_ID,
            "entitlement_mode": "unlimited",
            "status": "active",
            "smoke_fixture": SMOKE_FIXTURE_VERSION,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    await _ensure_document(
        account_ref.collection("entitlements").document("current"),
        {
            "schema_version": 1,
            "accepted_image_count": 0,
            "entitlement_mode": "unlimited",
            "trial_image_limit": None,
            "smoke_fixture": SMOKE_FIXTURE_VERSION,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    await _ensure_document(
        client.collection("identities").document(SMOKE_OWNER_ID),
        {
            "schema_version": 1,
            "account_id": SMOKE_ACCOUNT_ID,
            "account_class": "internal_smoke",
            "status": "active",
            "smoke_fixture": SMOKE_FIXTURE_VERSION,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    await _ensure_document(
        account_ref.collection("captures").document(SMOKE_CAPTURE_ID),
        {
            "schema_version": 1,
            "id": SMOKE_CAPTURE_ID,
            "account_id": SMOKE_ACCOUNT_ID,
            "status": "stored",
            "smoke_fixture": SMOKE_FIXTURE_VERSION,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    event_ref = account_ref.collection("events").document(SMOKE_EVENT_ID)
    event_snapshot = await event_ref.get()
    if not event_snapshot.exists:
        event = ActivityEvent(
            id=SMOKE_EVENT_ID,
            account_id=SMOKE_ACCOUNT_ID,
            camera_ids=[SMOKE_EVENT_CAMERA_ID],
            first_capture_at=created_at,
            last_capture_at=created_at,
            capture_count=1,
            grouping_policy_version=SMOKE_FIXTURE_VERSION,
            created_at=created_at,
            updated_at=created_at,
        )
        capture = CaptureRecord(
            id=SMOKE_EVENT_CAPTURE_ID,
            account_id=SMOKE_ACCOUNT_ID,
            camera_id=SMOKE_EVENT_CAMERA_ID,
            idempotency_key="repository-smoke-event-idempotency-v1",
            content_type="image/jpeg",
            content_sha256="0" * 64,
            object_key=(f"accounts/{SMOKE_ACCOUNT_ID}/captures/{SMOKE_EVENT_CAPTURE_ID}.jpg"),
            event_id=SMOKE_EVENT_ID,
            status=CaptureStatus.STORED,
            created_at=created_at,
        )
        job = DurableJob(
            id=event_inference_job_id(SMOKE_EVENT_ID),
            account_id=SMOKE_ACCOUNT_ID,
            kind=JobKind.EVENT_INFERENCE,
            subject_id=SMOKE_EVENT_ID,
            subject_revision=event.current_revision,
            created_at=created_at,
        )
        capture_data = capture.model_dump(mode="python", exclude={"idempotency_key"})
        capture_data.update(
            schema_version=1,
            idempotency_hash="repository-smoke-event-idempotency-hash-v1",
            smoke_fixture=SMOKE_FIXTURE_VERSION,
        )
        await _ensure_document(
            account_ref.collection("captures").document(capture.id),
            capture_data,
        )
        await _ensure_document(
            account_ref.collection("jobs").document(job.id),
            {
                **job.model_dump(mode="python"),
                "schema_version": 1,
                "smoke_fixture": SMOKE_FIXTURE_VERSION,
            },
        )
        await _ensure_document(
            event_ref,
            {
                **event.model_dump(mode="python"),
                "schema_version": 1,
                "smoke_fixture": SMOKE_FIXTURE_VERSION,
            },
        )
    elif event_snapshot.get("grouping_policy_version") != SMOKE_FIXTURE_VERSION:
        raise RuntimeError("Event-publication smoke fixture identity collided")


def _publication_hypothesis() -> ActivityMealInferenceV1:
    return ActivityMealInferenceV1.model_validate(
        {
            "schema_version": "activity-meal-inference-v1",
            "event_id": SMOKE_EVENT_ID,
            "source_capture_ids": [SMOKE_EVENT_CAPTURE_ID],
            "kind": "tentative_meal",
            "best_guess": "Smoke-test meal candidate",
            "confidence": "uncertain",
            "components": [
                {
                    "id": "candidate",
                    "name": "Smoke-test meal candidate",
                    "ingredients": [],
                    "preparation_methods": [],
                    "confidence": "uncertain",
                    "alternatives": [],
                    "evidence_ids": ["obs_fixture"],
                }
            ],
            "direct_observations": [
                {
                    "id": "obs_fixture",
                    "description": "An isolated synthetic fixture exercises publication.",
                    "image_evidence": [{"capture_id": SMOKE_EVENT_CAPTURE_ID}],
                }
            ],
            "contextual_evidence": [],
            "assumptions": [],
            "deductions": [],
            "alternatives": [],
            "rationale": "This deterministic fixture validates persistence, not food recognition.",
            "question": None,
            "allowed_actions": [
                "confirm_guess",
                "correct",
                "discard_not_cooking",
            ],
        }
    )


async def _publish_or_verify_hypothesis(repository: Repository) -> MealEntry:
    event, _ = await repository.event_evidence_for_account(
        account_id=SMOKE_ACCOUNT_ID,
        event_id=SMOKE_EVENT_ID,
    )
    claimed = await repository.claim_job(
        account_id=SMOKE_ACCOUNT_ID,
        job_id=event_inference_job_id(event.id),
        expected_subject_revision=event.current_revision,
        lease_id=SMOKE_EVENT_LEASE_ID,
        lease_owner=SMOKE_EVENT_WORKER_ID,
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    if claimed is not None:
        published = await repository.publish_event_inference(
            account_id=SMOKE_ACCOUNT_ID,
            event_id=event.id,
            expected_event_revision=event.current_revision,
            lease_id=SMOKE_EVENT_LEASE_ID,
            lease_owner=SMOKE_EVENT_WORKER_ID,
            hypothesis=_publication_hypothesis(),
        )
        if published is None:
            raise RuntimeError("Event-publication smoke lost its active lease")
        return published
    stored = await repository.meal_for_owner(SMOKE_OWNER_ID, SMOKE_EVENT_ID)
    if stored.activity_hypothesis != _publication_hypothesis():
        raise RuntimeError("Stored event-publication smoke hypothesis changed")
    return stored


async def run_smoke(repository: Repository) -> dict[str, Any]:
    published_hypothesis = await _publish_or_verify_hypothesis(repository)
    initial_meal = MealEntry(
        id=SMOKE_MEAL_ID,
        account_id=SMOKE_ACCOUNT_ID,
        capture_id=SMOKE_CAPTURE_ID,
        title="Likely air-fried steak",
        confidence=Confidence.UNCERTAIN,
        components=[
            MealComponent(
                name="Red meat",
                ingredients=["red meat"],
                preparation_methods=["air frying"],
            )
        ],
        observations=["Red meat is visible beside an air-fryer basket."],
        alternatives=["Air-fried lamb"],
        rationale="The meat appears red, but the distant view does not show the cut.",
        clarification_question="Was this steak or lamb?",
        clarification_reason="The answer distinguishes the supported red-meat options.",
    )
    meal = await repository.save_meal(account_id=SMOKE_ACCOUNT_ID, meal=initial_meal)
    question = await repository.open_question(
        account_id=SMOKE_ACCOUNT_ID,
        meal=meal,
        prompt="Was this steak or lamb?",
        reason="The distant view supports both options.",
    )
    confirmation = await repository.record_meal_feedback(
        owner_user_id=SMOKE_OWNER_ID,
        meal_id=meal.id,
        request=MealFeedbackRequest(kind=MealFeedbackKind.CONFIRM),
        idempotency_key="repository-smoke-confirm-v1",
    )
    answer = await repository.answer_question(
        owner_user_id=SMOKE_OWNER_ID,
        question_id=question.id,
        request=QuestionAnswerRequest(
            answer="Steak",
            learning_tip="The beef packet has a dark green label.",
        ),
        idempotency_key="repository-smoke-answer-v1",
    )
    targeted = await repository.record_meal_feedback(
        owner_user_id=SMOKE_OWNER_ID,
        meal_id=meal.id,
        request=MealFeedbackRequest.model_validate(
            {
                "kind": "correct",
                "base_revision_number": 3,
                "correction": {
                    "scope": "component",
                    "component_index": 0,
                    "replacement": {
                        "name": "Ribeye steak",
                        "ingredients": ["beef ribeye"],
                        "preparation_methods": ["air frying"],
                    },
                },
                "explanation": "The isolated smoke fixture identifies the cut as ribeye.",
            }
        ),
        idempotency_key="repository-smoke-targeted-component-v1",
    )
    learning = await FeedbackLearningService(repository).record(
        owner_user_id=SMOKE_OWNER_ID,
        meal_id=meal.id,
        request=MealFeedbackRequest(
            kind=MealFeedbackKind.CORRECT,
            actual_meal="Ribeye steak",
            explanation="The dark green beef label identifies this package as ribeye steak.",
            learning_disposition=MealFeedbackLearningDisposition.REUSABLE,
        ),
        idempotency_key="repository-smoke-reusable-feedback-v1",
    )
    not_cooking = await FeedbackLearningService(repository).record(
        owner_user_id=SMOKE_OWNER_ID,
        meal_id=meal.id,
        request=MealFeedbackRequest(
            kind=MealFeedbackKind.NOT_COOKING,
            explanation="The isolated smoke disposition verifies journal exclusion.",
        ),
        idempotency_key="repository-smoke-not-cooking-v1",
    )
    after_disposition = await repository.meal_for_owner(SMOKE_OWNER_ID, meal.id)
    journal_after_disposition = await repository.list_meals(SMOKE_OWNER_ID)
    journal_contains_meal = any(item.id == meal.id for item in journal_after_disposition)
    if after_disposition.revision_number == 6 and journal_contains_meal:
        raise RuntimeError("Not-cooking meal remained in the journal projection")
    if after_disposition.revision_number == 7 and not journal_contains_meal:
        raise RuntimeError("Historical disposition retry rolled back the current journal")
    if after_disposition.revision_number not in {6, 7}:
        raise RuntimeError("Not-cooking smoke found an unexpected materialized revision")
    reclassified = await FeedbackLearningService(repository).record(
        owner_user_id=SMOKE_OWNER_ID,
        meal_id=meal.id,
        request=MealFeedbackRequest(
            kind=MealFeedbackKind.CORRECT,
            actual_meal="Ribeye steak",
            explanation="The smoke explicitly restores the discarded event as cooking.",
        ),
        idempotency_key="repository-smoke-reclassify-v1",
    )
    current = await repository.meal_for_owner(SMOKE_OWNER_ID, meal.id)
    revisions = await repository.list_meal_revisions(SMOKE_OWNER_ID, meal.id)
    questions = await repository.list_questions(
        SMOKE_OWNER_ID,
        question_status=QuestionStatus.ANSWERED,
    )
    if confirmation.revision.number != 2:
        raise RuntimeError("Confirmation did not persist immutable revision 2")
    if answer.revision.number != 3:
        raise RuntimeError("Question answer did not atomically persist revision 3")
    if (
        targeted.revision.number != 4
        or targeted.revision.base_revision_number != 3
        or targeted.revision.correction is None
        or targeted.revision.correction.scope != "component"
        or current.revision_number != 7
        or current.components[0].name != "Ribeye steak"
    ):
        raise RuntimeError("Targeted correction did not atomically persist revision 4")
    if [revision.number for revision in revisions] != [1, 2, 3, 4, 5, 6, 7]:
        raise RuntimeError("Repository smoke revision history is incomplete")
    if (
        learning.revision.number != 5
        or learning.knowledge is None
        or learning.knowledge.revision.lifecycle != KnowledgeLifecycle.CONFIRMED
    ):
        raise RuntimeError("Reusable feedback did not apply one knowledge revision")
    if (
        not_cooking.revision.number != 6
        or not_cooking.revision.status != "not_cooking"
        or reclassified.revision.number != 7
        or reclassified.revision.status != "corrected"
        or current.title != "Ribeye steak"
        or not any(item.id == meal.id for item in await repository.list_meals(SMOKE_OWNER_ID))
    ):
        raise RuntimeError("Not-cooking disposition or explicit reclassification failed")
    if len(questions) != 1 or questions[0].id != question.id:
        raise RuntimeError("Repository smoke question did not close exactly once")
    initial_knowledge = await repository.record_knowledge_revision(
        account_id=SMOKE_ACCOUNT_ID,
        topic_key="Repository smoke household pattern",
        expected_revision_number=None,
        draft=KnowledgeRevisionDraft(
            title="Repository smoke household pattern",
            statement="The isolated smoke meal may represent a household pattern.",
            lifecycle=KnowledgeLifecycle.INFERRED,
            belief_strength=KnowledgeBeliefStrength.WEAK,
            source=KnowledgeRevisionSource.AGENT_INFERENCE,
            evidence=[
                KnowledgeEvidenceReference(
                    kind=KnowledgeEvidenceKind.MEAL_REVISION,
                    id=targeted.revision.id,
                    role=KnowledgeEvidenceRole.SUPPORTS,
                )
            ],
            reason="The deterministic smoke meal supplies bounded source provenance.",
        ),
        idempotency_key="repository-smoke-knowledge-inferred-v1",
    )
    reinforced_knowledge = await repository.record_knowledge_revision(
        account_id=SMOKE_ACCOUNT_ID,
        topic_key=initial_knowledge.page.topic_key,
        expected_revision_number=1,
        draft=KnowledgeRevisionDraft(
            title="Repository smoke household pattern",
            statement="The isolated smoke meal represents a reinforced household pattern.",
            lifecycle=KnowledgeLifecycle.REINFORCED,
            belief_strength=KnowledgeBeliefStrength.MODERATE,
            source=KnowledgeRevisionSource.USER_FEEDBACK,
            evidence=[
                KnowledgeEvidenceReference(
                    kind=KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                    id=initial_knowledge.revision.id,
                    role=KnowledgeEvidenceRole.CONTEXT,
                ),
                KnowledgeEvidenceReference(
                    kind=KnowledgeEvidenceKind.FEEDBACK,
                    id=answer.feedback.id,
                    role=KnowledgeEvidenceRole.SUPPORTS,
                ),
            ],
            reason="The deterministic user feedback reinforces the isolated belief.",
        ),
        idempotency_key="repository-smoke-knowledge-reinforced-v1",
    )
    knowledge_revisions = await repository.list_knowledge_revisions(
        SMOKE_OWNER_ID,
        initial_knowledge.page.id,
    )
    if (
        reinforced_knowledge.page.current_revision_number != 2
        or reinforced_knowledge.page.lifecycle != KnowledgeLifecycle.REINFORCED
        or [revision.number for revision in knowledge_revisions] != [1, 2]
        or knowledge_revisions[1].previous_revision_id != knowledge_revisions[0].id
    ):
        raise RuntimeError("Versioned household knowledge did not persist revisions 1 and 2")
    updater = HouseholdKnowledgeUpdater(repository)
    proposed_claim = KnowledgeClaim(
        dimension="package cue",
        value="dark green beef label indicates steak",
        conditions=("air fryer basket by sink",),
    )
    proposed_inference = await updater.apply(
        account_id=SMOKE_ACCOUNT_ID,
        proposal=KnowledgeUpdateProposal(
            topic_key="Repository smoke proposed household knowledge",
            title="Repository smoke package cue",
            statement="A dark green beef label may indicate steak in this household.",
            claim=proposed_claim,
            intent=KnowledgeUpdateIntent.INFER,
            source=KnowledgeRevisionSource.AGENT_INFERENCE,
            evidence=(
                KnowledgeEvidenceReference(
                    kind=KnowledgeEvidenceKind.MEAL_REVISION,
                    id=targeted.revision.id,
                    role=KnowledgeEvidenceRole.SUPPORTS,
                ),
            ),
            reason="The corrected smoke meal weakly supports a reusable package cue.",
        ),
        expected_revision_number=None,
        idempotency_key="repository-smoke-proposed-knowledge-inferred-v1",
    )
    feedback_support = KnowledgeEvidenceReference(
        kind=KnowledgeEvidenceKind.FEEDBACK,
        id=answer.feedback.id,
        role=KnowledgeEvidenceRole.SUPPORTS,
    )
    proposed_confirmation = await updater.apply(
        account_id=SMOKE_ACCOUNT_ID,
        proposal=KnowledgeUpdateProposal(
            topic_key=proposed_inference.page.topic_key,
            title="Repository smoke package cue",
            statement="A dark green beef label indicates steak in this household.",
            claim=proposed_claim,
            intent=KnowledgeUpdateIntent.CONFIRM,
            source=KnowledgeRevisionSource.USER_FEEDBACK,
            evidence=(
                KnowledgeEvidenceReference(
                    kind=KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                    id=proposed_inference.revision.id,
                    role=KnowledgeEvidenceRole.CONTEXT,
                ),
                feedback_support,
            ),
            confirmed_sources=(
                ConfirmedClaimSource(claim=proposed_claim, evidence=feedback_support),
            ),
            reason="The exact user feedback confirms the bounded package cue.",
        ),
        expected_revision_number=1,
        idempotency_key="repository-smoke-proposed-knowledge-confirmed-v1",
    )
    proposed_revisions = await repository.list_knowledge_revisions(
        SMOKE_OWNER_ID,
        proposed_inference.page.id,
    )
    if (
        proposed_confirmation.page.lifecycle != KnowledgeLifecycle.CONFIRMED
        or proposed_confirmation.page.claim != proposed_claim
        or [revision.number for revision in proposed_revisions] != [1, 2]
    ):
        raise RuntimeError("Validated household knowledge proposal did not persist exactly once")
    knowledge_index = await repository.knowledge_page_index_for_account(
        account_id=SMOKE_ACCOUNT_ID,
    )
    selected_knowledge = await repository.active_knowledge_revision_for_account(
        account_id=SMOKE_ACCOUNT_ID,
        page_id=proposed_confirmation.page.id,
    )
    indexed_page_ids = [page.id for page in knowledge_index]
    expected_page_ids = {
        initial_knowledge.page.id,
        proposed_confirmation.page.id,
        learning.knowledge.page.id,
    }
    if not expected_page_ids.issubset(indexed_page_ids):
        raise RuntimeError("Household knowledge index omitted an active smoke page")
    if (
        selected_knowledge is None
        or selected_knowledge.page != proposed_confirmation.page
        or selected_knowledge.revision != proposed_confirmation.revision
    ):
        raise RuntimeError("Exact household knowledge read did not return its current revision")
    return {
        "schema_version": SMOKE_FIXTURE_VERSION,
        "account_id": SMOKE_ACCOUNT_ID,
        "capture_id": SMOKE_CAPTURE_ID,
        "meal_id": SMOKE_MEAL_ID,
        "meal_revision": current.revision_number,
        "revision_numbers": [revision.number for revision in revisions],
        "question_status": questions[0].status,
        "feedback_revision_numbers": [
            confirmation.revision.number,
            answer.revision.number,
            targeted.revision.number,
            learning.revision.number,
            not_cooking.revision.number,
            reclassified.revision.number,
        ],
        "not_cooking_revision": not_cooking.revision.number,
        "reclassified_revision": reclassified.revision.number,
        "published_event_id": published_hypothesis.event_id,
        "published_classification": published_hypothesis.activity_hypothesis.kind,
        "published_revision": published_hypothesis.revision_number,
        "knowledge_page_id": initial_knowledge.page.id,
        "knowledge_lifecycle": reinforced_knowledge.page.lifecycle,
        "knowledge_revision_numbers": [revision.number for revision in knowledge_revisions],
        "proposed_knowledge_page_id": proposed_inference.page.id,
        "proposed_knowledge_lifecycle": proposed_confirmation.page.lifecycle,
        "proposed_knowledge_revision_numbers": [revision.number for revision in proposed_revisions],
        "feedback_knowledge_page_id": learning.knowledge.page.id,
        "feedback_knowledge_revision": learning.knowledge.revision.number,
        "knowledge_index_count": len(knowledge_index),
        "knowledge_index_page_ids": indexed_page_ids,
        "selected_knowledge_revision_id": selected_knowledge.revision.id,
        "selected_knowledge_revision_number": selected_knowledge.revision.number,
        "model_calls": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify production Firestore meal, question, feedback, and revision writes."
    )
    parser.add_argument(
        "--confirm-isolated-smoke",
        action="store_true",
        help="Confirm this execution may create or reuse the isolated smoke fixture.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_isolated_smoke:
        _parser().error("--confirm-isolated-smoke is required")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required")
    client = AsyncClient(project=project_id)
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
        client=client,
    )

    async def execute() -> dict[str, Any]:
        await ensure_smoke_fixture(client)
        return await run_smoke(repository)

    try:
        print(json.dumps(asyncio.run(execute()), indent=2, sort_keys=True))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
