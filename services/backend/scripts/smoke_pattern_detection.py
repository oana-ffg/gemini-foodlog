from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from google.cloud.firestore_v1 import DELETE_FIELD
from synthetic_dataset_support import publish_synthetic_event, seed_synthetic_meal

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.grouping import CaptureGroupingService, GroupingPolicy
from foodlog_backend.image_events import CaptureStoredEventV1, PubSubCaptureEventPublisher
from foodlog_backend.models import (
    ClarificationQuestion,
    DurableJob,
    JobStatus,
    MealEntry,
    MealFeedbackKind,
    MealFeedbackRequest,
    QuestionResponseKind,
    event_inference_job_id,
    utc_now,
)
from foodlog_backend.pattern_detection import PatternDetectionService
from foodlog_backend.storage import GCSObjectStore

SYNTHETIC_MARKER = "Synthetic longitudinal pattern smoke"
LOCAL_TIMEZONE = timezone(timedelta(hours=2))


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected_status: int,
    headers: dict[str, str] | None = None,
    json: dict[str, object] | None = None,
) -> object:
    response = client.request(method, path, headers=headers, json=json)
    assert response.status_code == expected_status, (
        f"{method} {path}: expected {expected_status}, got {response.status_code}: "
        f"{response.text}"
    )
    return response.json()


async def invoke_deployed_detector(
    *,
    publisher: PubSubCaptureEventPublisher,
    account_id: str,
    capture_id: str,
) -> str:
    return await publisher.publish(
        CaptureStoredEventV1(account_id=account_id, capture_id=capture_id)
    )


async def wait_for_open_pattern(
    client: httpx.Client,
    *,
    predecessor_id: str | None = None,
) -> ClarificationQuestion:
    for _ in range(30):
        visible = request_json(client, "GET", "/v1/questions", expected_status=200)
        assert isinstance(visible, list)
        matches = [
            ClarificationQuestion.model_validate(question)
            for question in visible
            if question.get("pattern_claim") is not None
            and SYNTHETIC_MARKER.casefold()
            in question["pattern_claim"]["value"].casefold()
            and (
                predecessor_id is None
                or question.get("predecessor_question_id") == predecessor_id
            )
        ]
        if matches:
            return matches[0]
        await asyncio.sleep(1)
    raise AssertionError("deployed detector did not surface the expected pattern in 30 seconds")


async def recover_incomplete_smoke_captures(
    repository: FirestoreRepository,
    grouping: CaptureGroupingService,
    *,
    account_id: str,
) -> list[str]:
    recovered: list[str] = []
    collection = repository._client.collection("accounts").document(account_id).collection(
        "captures"
    )
    async for snapshot in collection.stream():
        data = snapshot.to_dict() or {}
        metadata = data.get("metadata") or {}
        if (
            metadata.get("client_version") != "pattern-production-smoke-v1"
            or data.get("status") != "stored"
        ):
            continue
        grouped = await grouping.process(
            account_id=account_id,
            capture_id=snapshot.id,
            worker_id="synthetic-pattern-smoke-recovery",
        )
        capture = await repository.capture_for_account(
            account_id=account_id,
            capture_id=snapshot.id,
        )
        if grouped is None:
            assert capture.event_id is not None
            event, captures = await repository.event_evidence_for_account(
                account_id=account_id,
                event_id=capture.event_id,
            )
            job_ref = (
                repository._client.collection("accounts")
                .document(account_id)
                .collection("jobs")
                .document(event_inference_job_id(event.id))
            )
            job_snapshot = await job_ref.get()
            job = DurableJob.model_validate(
                {key: value for key, value in (job_snapshot.to_dict() or {}).items()}
            )
            assert job.status == JobStatus.COMPLETED
            assert event.status == "open" and event.meal_id is None
            await job_ref.update(
                {
                    "status": JobStatus.PENDING.value,
                    "attempt_count": 0,
                    "available_at": utc_now(),
                    "lease_id": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error_code": None,
                    "last_error_message": None,
                    "completed_at": DELETE_FIELD,
                }
            )
        else:
            event, captures = await repository.event_evidence_for_account(
                account_id=account_id,
                event_id=grouped.event.id,
            )
        await publish_synthetic_event(
            repository,
            event=event,
            captures=captures,
            title=f"{SYNTHETIC_MARKER} Recovered",
            evidence_description=f"{SYNTHETIC_MARKER} retained evidence.",
            rationale="Synthetic no-model production detector smoke.",
            lease_owner="synthetic-pattern-smoke-publication",
        )
        recovered.append(snapshot.id)
    return recovered


async def smoke(args: argparse.Namespace) -> None:
    email = os.environ["FOODLOG_SMOKE_EMAIL"]
    password = os.environ["FOODLOG_SMOKE_PASSWORD"]
    firebase_response = httpx.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
        params={"key": args.firebase_api_key},
        headers={"Origin": args.origin, "Referer": f"{args.origin}/"},
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=30,
    )
    firebase_response.raise_for_status()
    firebase_payload = firebase_response.json()
    token = firebase_payload["idToken"]
    owner_user_id = firebase_payload["localId"]
    run_id = uuid4().hex[:12]

    repository = FirestoreRepository(
        project_id=args.project,
        public_account_limit=25,
        trial_image_limit=200,
    )
    store = GCSObjectStore(project_id=args.project, bucket_name=args.bucket)
    grouping = CaptureGroupingService(
        repository=repository,
        policy=GroupingPolicy(version="pattern-production-smoke-v1"),
    )
    publisher = PubSubCaptureEventPublisher(topic=args.image_topic)
    detector = PatternDetectionService(repository)
    ledger_before = (
        await repository._client.collection("system").document("model_spend").get()
    ).to_dict()

    try:
        account = await repository.provision_account(owner_user_id)
        camera = await repository.create_browser_camera(
            owner_user_id,
            "Longitudinal pattern smoke camera",
            "longitudinal-pattern-smoke-browser-v1",
        )
        recovered_capture_ids = await recover_incomplete_smoke_captures(
            repository,
            grouping,
            account_id=account.id,
        )
        recovered_meal_count = 0
        for meal in await repository.list_meals(owner_user_id):
            if SYNTHETIC_MARKER not in meal.title:
                continue
            await repository.record_meal_feedback(
                owner_user_id=owner_user_id,
                meal_id=meal.id,
                request=MealFeedbackRequest(
                    kind=MealFeedbackKind.NOT_COOKING,
                    explanation="Recovered synthetic detector smoke cleanup.",
                ),
                idempotency_key=f"pattern-recovery-meal-cleanup-{meal.id}",
            )
            recovered_meal_count += 1
        with httpx.Client(
            base_url=args.api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": args.origin,
                "Referer": f"{args.origin}/",
            },
            timeout=30,
        ) as client:
            open_questions = request_json(
                client,
                "GET",
                "/v1/questions",
                expected_status=200,
            )
            assert isinstance(open_questions, list)
            for question in open_questions:
                if SYNTHETIC_MARKER not in (question.get("tentative_claim") or ""):
                    continue
                request_json(
                    client,
                    "POST",
                    f"/v1/questions/{question['id']}/responses",
                    expected_status=200,
                    headers={
                        "Idempotency-Key": f"pattern-detector-recover-{question['id']}"
                    },
                    json={
                        "kind": QuestionResponseKind.REJECT.value,
                        "explanation": "Recovered synthetic detector smoke cleanup.",
                    },
                )

            image = args.fixture.read_bytes()
            assert image.startswith(b"\x89PNG\r\n\x1a\n")
            titles = (
                f"{SYNTHETIC_MARKER} Steak",
                f"{SYNTHETIC_MARKER} Steak",
                f"{SYNTHETIC_MARKER} Steak",
                f"{SYNTHETIC_MARKER} Chicken",
                f"{SYNTHETIC_MARKER} Steak",
                f"{SYNTHETIC_MARKER} Steak",
            )
            first_thursday = datetime(
                2026,
                7,
                9 if recovered_capture_ids else 2,
                18,
                tzinfo=LOCAL_TIMEZONE,
            )
            meals: list[MealEntry] = []
            original_question = None
            resurfaced_question = None
            deployed_message_ids: list[str] = []
            for ordinal, title in enumerate(titles, start=1):
                meals.append(
                    await seed_synthetic_meal(
                        repository,
                        store,
                        grouping,
                        account=account,
                        camera=camera,
                        image=image,
                        local_at=first_thursday + timedelta(weeks=ordinal - 1),
                        title=title,
                        sequence_id=f"pattern-smoke-{run_id}",
                        sequence_number=ordinal,
                        idempotency_key=f"pattern-detection-smoke-{run_id}-{ordinal}",
                        client_version="pattern-production-smoke-v1",
                        worker_id="synthetic-pattern-smoke",
                        lease_owner="synthetic-pattern-smoke-publication",
                        evidence_description=f"{SYNTHETIC_MARKER} retained evidence.",
                        rationale="Synthetic no-model production detector smoke.",
                    )
                )
                assert meals[-1].occurred_utc_offset_minutes == 120
                if ordinal == 4:
                    deployed_message_ids.append(
                        await invoke_deployed_detector(
                            publisher=publisher,
                            account_id=account.id,
                            capture_id=meals[-1].capture_id,
                        )
                    )
                    original_question = await wait_for_open_pattern(client)
                    assert len(original_question.pattern_supporting_examples) == 3
                    assert len(original_question.pattern_counterexamples) == 1
                    request_json(
                        client,
                        "POST",
                        f"/v1/questions/{original_question.id}/responses",
                        expected_status=200,
                        headers={"Idempotency-Key": f"pattern-detect-reject-{run_id}"},
                        json={"kind": "reject", "explanation": "Synthetic smoke rejection."},
                    )
                elif ordinal == 5:
                    one_new = await detector.detect_and_propose(
                        account_id=account.id,
                        max_proposals=5,
                    )
                    assert not any(
                        question.pattern_claim is not None
                        and SYNTHETIC_MARKER.casefold()
                        in question.pattern_claim.value.casefold()
                        for question in one_new
                    )
                elif ordinal == 6:
                    deployed_message_ids.append(
                        await invoke_deployed_detector(
                            publisher=publisher,
                            account_id=account.id,
                            capture_id=meals[-1].capture_id,
                        )
                    )
                    assert original_question is not None
                    resurfaced_question = await wait_for_open_pattern(
                        client,
                        predecessor_id=original_question.id,
                    )
                    assert resurfaced_question.id != original_question.id
                    assert resurfaced_question.predecessor_question_id == original_question.id
                    request_json(
                        client,
                        "POST",
                        f"/v1/questions/{resurfaced_question.id}/responses",
                        expected_status=200,
                        headers={"Idempotency-Key": f"pattern-resurface-reject-{run_id}"},
                        json={"kind": "reject", "explanation": "Synthetic smoke cleanup."},
                    )

            for meal in meals:
                await repository.record_meal_feedback(
                    owner_user_id=owner_user_id,
                    meal_id=meal.id,
                    request=MealFeedbackRequest(
                        kind=MealFeedbackKind.NOT_COOKING,
                        explanation="Synthetic longitudinal detector smoke cleanup.",
                    ),
                    idempotency_key=f"pattern-meal-cleanup-{meal.id}",
                )
            assert await detector.detect_and_propose(account_id=account.id) == []
            remaining = request_json(
                client,
                "GET",
                "/v1/questions",
                expected_status=200,
            )
            assert isinstance(remaining, list)
            assert not any(
                question.get("pattern_claim") is not None
                and SYNTHETIC_MARKER.casefold()
                in question["pattern_claim"]["value"].casefold()
                for question in remaining
            )

        ledger_after = (
            await repository._client.collection("system").document("model_spend").get()
        ).to_dict()
        assert ledger_after == ledger_before
        assert original_question is not None
        assert resurfaced_question is not None
        print(f"account_id={account.id}")
        print(f"original_question_id={original_question.id}")
        print(f"resurfaced_question_id={resurfaced_question.id}")
        print("supporting_examples=3")
        print("counterexamples=1")
        print("local_utc_offset_minutes=120")
        print("rejected_resurface_threshold=2")
        print("synthetic_meals_discarded=true")
        print(f"recovered_incomplete_capture_count={len(recovered_capture_ids)}")
        print(f"recovered_active_meal_count={recovered_meal_count}")
        print("model_spend_ledger_unchanged=true")
        print(f"deployed_pubsub_message_ids={','.join(deployed_message_ids)}")
        print("deployed_inference_worker_invoked=true")
        print("model_calls=0")
    finally:
        repository._client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--image-topic", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--fixture", required=True, type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(smoke(parse_args()))
