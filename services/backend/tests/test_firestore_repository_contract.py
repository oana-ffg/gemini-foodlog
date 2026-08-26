from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from hashlib import sha256

import pytest
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.errors import IdempotencyConflict, QuestionAlreadyAnswered
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.firestore_repository_smoke import (
    SMOKE_ACCOUNT_ID,
    ensure_smoke_fixture,
    run_smoke,
)
from foodlog_backend.inference_schema import ActivityMealInferenceV1
from foodlog_backend.models import (
    ActivityEvent,
    ActivityEventStatus,
    CaptureRecord,
    CaptureStatus,
    Confidence,
    DurableJob,
    JobKind,
    JobStatus,
    MealComponent,
    MealEntry,
    MealFeedbackKind,
    MealFeedbackRequest,
    MealStatus,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionStatus,
    event_inference_job_id,
    utc_now,
)
from tests.inference_fixtures import base_payload


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_meal_question_feedback_and_revisions_are_atomic() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-repository-contract-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner_user_id = "firestore-contract-owner"
        account = await repository.provision_account(owner_user_id)
        capture_id = "firestore-contract-capture"
        await (
            client.collection("accounts")
            .document(account.id)
            .collection("captures")
            .document(capture_id)
            .set(
                {
                    "schema_version": 1,
                    "id": capture_id,
                    "account_id": account.id,
                    "status": "stored",
                }
            )
        )
        meal = MealEntry(
            id="firestore-contract-meal",
            account_id=account.id,
            capture_id=capture_id,
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

        stored = await repository.save_meal(account_id=account.id, meal=meal)
        retry = await repository.save_meal(account_id=account.id, meal=meal)
        assert retry == stored == meal

        questions = await asyncio.gather(
            *(
                repository.open_question(
                    account_id=account.id,
                    meal=meal,
                    prompt="Was this steak or lamb?",
                    reason="The distant view supports both options.",
                )
                for _ in range(2)
            )
        )
        assert questions[0] == questions[1]

        confirmation_request = MealFeedbackRequest(kind=MealFeedbackKind.CONFIRM)
        confirmation = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=confirmation_request,
            idempotency_key="firestore-contract-confirm",
        )
        confirmation_retry = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=confirmation_request,
            idempotency_key="firestore-contract-confirm",
        )
        assert confirmation_retry == confirmation
        assert confirmation.revision.number == 2
        assert confirmation.revision.status == MealStatus.CONFIRMED

        outcomes = await asyncio.gather(
            repository.answer_question(
                owner_user_id=owner_user_id,
                question_id=meal.id,
                request=QuestionAnswerRequest(
                    answer="Steak",
                    learning_tip="The beef packet has a dark green label.",
                ),
                idempotency_key="firestore-contract-answer-steak",
            ),
            repository.answer_question(
                owner_user_id=owner_user_id,
                question_id=meal.id,
                request=QuestionAnswerRequest(answer="Lamb"),
                idempotency_key="firestore-contract-answer-lamb",
            ),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if isinstance(item, QuestionAnswerResult)]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        assert len(successes) == 1, repr(outcomes)
        assert len(failures) == 1, repr(outcomes)
        assert isinstance(failures[0], QuestionAlreadyAnswered)
        assert successes[0].revision.number == 3

        current = await repository.meal_for_owner(owner_user_id, meal.id)
        revisions = await repository.list_meal_revisions(owner_user_id, meal.id)
        answered = await repository.list_questions(
            owner_user_id,
            question_status=QuestionStatus.ANSWERED,
        )
        feedback = [
            snapshot
            async for snapshot in client.collection("accounts")
            .document(account.id)
            .collection("feedback")
            .stream()
        ]
        assert current.revision_number == 3
        assert [revision.number for revision in revisions] == [1, 2, 3]
        assert [revision.source for revision in revisions] == [
            "inference",
            "user_feedback",
            "user_feedback",
        ]
        assert len(answered) == 1
        assert answered[0].answer == successes[0].question.answer
        assert len(feedback) == 2
        assert all("idempotency_key" not in snapshot.to_dict() for snapshot in feedback)
        confirmation_ref = (
            client.collection("accounts")
            .document(account.id)
            .collection("feedback")
            .document(sha256(b"firestore-contract-confirm").hexdigest())
        )
        confirmation_snapshot = await confirmation_ref.get()
        confirmation_raw = confirmation_snapshot.to_dict() or {}
        assert confirmation_raw["actual_meal"] is None
        assert confirmation_raw["explanation"] is None
        assert confirmation_raw["question_id"] is None
        assert confirmation_raw["idempotency_hash"] == confirmation_snapshot.id

        answer_ref = (
            client.collection("accounts")
            .document(account.id)
            .collection("feedback")
            .document(successes[0].feedback.id)
        )
        answer_raw = (await answer_ref.get()).to_dict() or {}
        assert answer_raw["actual_meal"] == successes[0].question.answer
        assert answer_raw["explanation"] == successes[0].question.learning_tip
        assert answer_raw["question_id"] == successes[0].question.id

        confirmation_retry_again = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=confirmation_request,
            idempotency_key="firestore-contract-confirm",
        )
        assert confirmation_retry_again == confirmation
        assert (await confirmation_ref.get()).to_dict() == confirmation_raw
        with pytest.raises(IdempotencyConflict):
            await repository.record_meal_feedback(
                owner_user_id=owner_user_id,
                meal_id=meal.id,
                request=MealFeedbackRequest(
                    kind=MealFeedbackKind.CORRECT,
                    actual_meal="Different payload",
                ),
                idempotency_key="firestore-contract-confirm",
            )
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_event_publication_atomically_fences_lease_and_revision() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-event-publication-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        account = await repository.provision_account("event-publication-owner")
        now = utc_now()
        event = ActivityEvent(
            id="event-publication-event",
            account_id=account.id,
            camera_ids=["event-publication-camera"],
            first_capture_at=now,
            last_capture_at=now,
            capture_count=1,
            grouping_policy_version="publication-test-v1",
        )
        capture = CaptureRecord(
            id="event-publication-capture",
            account_id=account.id,
            camera_id=event.camera_ids[0],
            idempotency_key="event-publication-idempotency",
            content_type="image/jpeg",
            content_sha256="a" * 64,
            object_key=(
                f"accounts/{account.id}/captures/event-publication-capture.jpg"
            ),
            event_id=event.id,
            status=CaptureStatus.STORED,
            created_at=now,
        )
        job = DurableJob(
            id=event_inference_job_id(event.id),
            account_id=account.id,
            kind=JobKind.EVENT_INFERENCE,
            subject_id=event.id,
            subject_revision=event.current_revision,
            status=JobStatus.LEASED,
            attempt_count=1,
            lease_id="event-publication-lease",
            lease_owner="event-publication-worker",
            lease_expires_at=now + timedelta(minutes=5),
            created_at=now,
        )
        account_ref = client.collection("accounts").document(account.id)
        capture_data = capture.model_dump(mode="python", exclude={"idempotency_key"})
        capture_data.update(schema_version=1, idempotency_hash="test-only")
        await account_ref.collection("events").document(event.id).set(
            {**event.model_dump(mode="python"), "schema_version": 1}
        )
        await account_ref.collection("captures").document(capture.id).set(capture_data)
        await account_ref.collection("jobs").document(job.id).set(
            {**job.model_dump(mode="python"), "schema_version": 1}
        )

        payload = base_payload()
        payload["event_id"] = event.id
        payload["source_capture_ids"] = [capture.id]
        payload["direct_observations"][0]["image_evidence"][0]["capture_id"] = capture.id
        hypothesis = ActivityMealInferenceV1.model_validate(payload)
        meal = await repository.publish_event_inference(
            account_id=account.id,
            event_id=event.id,
            expected_event_revision=event.current_revision,
            lease_id=job.lease_id or "",
            lease_owner=job.lease_owner or "",
            hypothesis=hypothesis,
        )
        assert meal is not None
        assert meal.activity_hypothesis == hypothesis

        duplicate = await repository.publish_event_inference(
            account_id=account.id,
            event_id=event.id,
            expected_event_revision=event.current_revision,
            lease_id=job.lease_id or "",
            lease_owner=job.lease_owner or "",
            hypothesis=hypothesis,
        )
        assert duplicate is None
        stored_event = await account_ref.collection("events").document(event.id).get()
        stored_job = await account_ref.collection("jobs").document(job.id).get()
        stored_capture = await account_ref.collection("captures").document(capture.id).get()
        revisions = [
            snapshot
            async for snapshot in account_ref.collection("meals")
            .document(meal.id)
            .collection("revisions")
            .stream()
        ]
        assert stored_event.get("status") == ActivityEventStatus.INFERRED
        assert stored_event.get("meal_id") == meal.id
        assert stored_job.get("status") == JobStatus.COMPLETED
        assert stored_capture.get("status") == CaptureStatus.PROCESSED
        assert len(revisions) == 1
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_deployed_repository_smoke_is_rerunnable_without_duplicate_revisions() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-repository-smoke-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        await ensure_smoke_fixture(client)
        await ensure_smoke_fixture(client)

        first = await run_smoke(repository)
        second = await run_smoke(repository)

        assert first == second
        assert first["revision_numbers"] == [1, 2, 3]
        assert first["model_calls"] == 0
        feedback = [
            snapshot
            async for snapshot in client.collection("accounts")
            .document(SMOKE_ACCOUNT_ID)
            .collection("feedback")
            .stream()
        ]
        assert len(feedback) == 2
        client.close()

    asyncio.run(scenario())
