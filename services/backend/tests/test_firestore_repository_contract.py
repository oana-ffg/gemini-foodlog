from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from hashlib import sha256

import pytest
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.errors import (
    IdempotencyConflict,
    KnowledgePageNotFound,
    KnowledgeRevisionConflict,
    MealRevisionConflict,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    QuestionSuperseded,
)
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
    KnowledgeBeliefStrength,
    KnowledgeClaim,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeLifecycle,
    KnowledgeRevisionDraft,
    KnowledgeRevisionResult,
    KnowledgeRevisionSource,
    MealComponent,
    MealEntry,
    MealFeedbackKind,
    MealFeedbackRequest,
    MealStatus,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionEvidenceKind,
    QuestionEvidenceReference,
    QuestionResponseKind,
    QuestionResponseRequest,
    QuestionResponseResult,
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
                question_id=questions[0].id,
                request=QuestionAnswerRequest(
                    answer="Steak",
                    learning_tip="The beef packet has a dark green label.",
                ),
                idempotency_key="firestore-contract-answer-steak",
            ),
            repository.answer_question(
                owner_user_id=owner_user_id,
                question_id=questions[0].id,
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

        targeted_request = MealFeedbackRequest.model_validate(
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
                "explanation": "The package showed ribeye.",
            }
        )
        targeted = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=targeted_request,
            idempotency_key="firestore-contract-targeted",
        )
        targeted_retry = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=targeted_request,
            idempotency_key="firestore-contract-targeted",
        )
        assert targeted_retry == targeted
        assert targeted.revision.number == 4
        assert targeted.revision.base_revision_number == 3
        assert targeted.revision.correction == targeted_request.correction
        assert targeted.revision.inference.components[0].name == "Ribeye steak"

        targeted_ref = (
            client.collection("accounts")
            .document(account.id)
            .collection("feedback")
            .document(sha256(b"firestore-contract-targeted").hexdigest())
        )
        targeted_raw = (await targeted_ref.get()).to_dict() or {}
        assert targeted_raw["base_revision_number"] == 3
        assert targeted_raw["correction"]["scope"] == "component"
        assert "idempotency_key" not in targeted_raw
        current = await repository.meal_for_owner(owner_user_id, meal.id)
        assert current.revision_number == 4
        assert current.components[0].name == "Ribeye steak"

        with pytest.raises(MealRevisionConflict):
            await repository.record_meal_feedback(
                owner_user_id=owner_user_id,
                meal_id=meal.id,
                request=targeted_request,
                idempotency_key="firestore-contract-stale-targeted",
            )
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_knowledge_revisions_are_atomic_provenanced_and_tenant_scoped() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-knowledge-contract-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner = await repository.provision_account("knowledge-contract-owner")
        await repository.provision_account("knowledge-contract-foreign-owner")
        initial_draft = KnowledgeRevisionDraft(
            title="Thursday dinner pattern",
            statement="Steak may often be eaten on Thursdays.",
            claim=KnowledgeClaim(
                dimension="likely meal",
                value="steak",
                conditions=("thursday",),
            ),
            lifecycle=KnowledgeLifecycle.INFERRED,
            belief_strength=KnowledgeBeliefStrength.WEAK,
            source=KnowledgeRevisionSource.AGENT_INFERENCE,
            evidence=[
                KnowledgeEvidenceReference(
                    kind=KnowledgeEvidenceKind.MEAL_REVISION,
                    id="knowledge-contract-meal-001",
                    role=KnowledgeEvidenceRole.SUPPORTS,
                )
            ],
            reason="One observed Thursday meal supports a tentative pattern.",
        )
        initial = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key="Thursday dinner pattern",
            expected_revision_number=None,
            draft=initial_draft,
            idempotency_key="knowledge-contract-initial",
        )
        initial_retry = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key="  thursday   dinner pattern ",
            expected_revision_number=None,
            draft=initial_draft,
            idempotency_key="knowledge-contract-initial",
        )
        assert initial_retry == initial

        drafts = [
            KnowledgeRevisionDraft(
                title="Thursday dinner pattern",
                statement=statement,
                claim=KnowledgeClaim(
                    dimension="likely meal",
                    value="steak" if index == 0 else "red meat",
                    conditions=("thursday",),
                ),
                lifecycle=KnowledgeLifecycle.REINFORCED,
                belief_strength=KnowledgeBeliefStrength.MODERATE,
                source=KnowledgeRevisionSource.AGENT_INFERENCE,
                evidence=[
                    KnowledgeEvidenceReference(
                        kind=KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                        id=initial.revision.id,
                        role=KnowledgeEvidenceRole.CONTEXT,
                    ),
                    KnowledgeEvidenceReference(
                        kind=KnowledgeEvidenceKind.MEAL_REVISION,
                        id=f"knowledge-contract-meal-00{index + 2}",
                        role=KnowledgeEvidenceRole.SUPPORTS,
                    ),
                ],
                reason="A second observed meal reinforces the tentative pattern.",
            )
            for index, statement in enumerate(
                [
                    "Steak is often eaten on Thursdays.",
                    "Red meat is often eaten on Thursdays.",
                ]
            )
        ]
        outcomes = await asyncio.gather(
            *(
                repository.record_knowledge_revision(
                    account_id=owner.id,
                    topic_key=initial.page.topic_key,
                    expected_revision_number=1,
                    draft=candidate,
                    idempotency_key=f"knowledge-contract-competing-{index}",
                )
                for index, candidate in enumerate(drafts)
            ),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if isinstance(item, KnowledgeRevisionResult)]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        assert len(successes) == 1, repr(outcomes)
        assert len(failures) == 1, repr(outcomes)
        assert isinstance(failures[0], KnowledgeRevisionConflict)
        winner_index = outcomes.index(successes[0])
        winner = successes[0]
        winner_retry = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key=initial.page.topic_key,
            expected_revision_number=1,
            draft=drafts[winner_index],
            idempotency_key=f"knowledge-contract-competing-{winner_index}",
        )
        assert winner_retry == winner

        page = await repository.knowledge_page_for_owner(
            "knowledge-contract-owner", initial.page.id
        )
        revisions = await repository.list_knowledge_revisions(
            "knowledge-contract-owner", initial.page.id
        )
        assert page.current_revision_number == 2
        assert page.current_revision_id == winner.revision.id
        assert [revision.number for revision in revisions] == [1, 2]
        assert revisions[0].statement == initial_draft.statement
        assert page.claim == winner.revision.claim
        assert revisions[0].claim == initial_draft.claim
        assert revisions[1].previous_revision_id == revisions[0].id
        assert any(
            item.kind == KnowledgeEvidenceKind.KNOWLEDGE_REVISION
            and item.id == revisions[0].id
            for item in revisions[1].evidence
        )

        account_ref = client.collection("accounts").document(owner.id)
        request_documents = [
            snapshot
            async for snapshot in account_ref.collection(
                "knowledge_revision_requests"
            ).stream()
        ]
        assert len(request_documents) == 2
        assert all("idempotency_key" not in item.to_dict() for item in request_documents)
        with pytest.raises(KnowledgePageNotFound):
            await repository.knowledge_page_for_owner(
                "knowledge-contract-foreign-owner", initial.page.id
            )
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_pattern_question_lifecycle_is_atomic_and_tenant_scoped() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-pattern-question-contract-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner = await repository.provision_account("pattern-owner")
        await repository.provision_account("pattern-foreign-owner")
        evidence = [
            QuestionEvidenceReference(
                kind=QuestionEvidenceKind.MEAL_REVISION,
                id="pattern-meal-revision-001",
            )
        ]
        first = await repository.open_pattern_question(
            account_id=owner.id,
            prompt="Steak appears most Thursdays. Is that accurate?",
            reason="Three Thursday meal revisions support the tentative pattern.",
            tentative_claim="Steak is usually eaten on Thursdays",
            evidence=evidence,
        )
        duplicate = await repository.open_pattern_question(
            account_id=owner.id,
            prompt="A differently worded duplicate should not create a record.",
            reason="The same normalized claim is already durable.",
            tentative_claim="  steak is usually eaten on THURSDAYS ",
            evidence=evidence,
        )
        assert duplicate.id == first.id

        replacement = await repository.open_pattern_question(
            account_id=owner.id,
            prompt="Steak appears late in the work week. Is that accurate?",
            reason="New evidence narrows the earlier pattern.",
            tentative_claim="Steak is usually eaten late in the work week",
            evidence=evidence,
            supersedes_question_id=first.id,
        )
        all_questions = await repository.list_questions(
            "pattern-owner",
            question_status=None,
        )
        old = next(question for question in all_questions if question.id == first.id)
        assert old.status == QuestionStatus.SUPERSEDED
        assert old.superseded_by_question_id == replacement.id
        with pytest.raises(QuestionSuperseded):
            await repository.respond_to_question(
                owner_user_id="pattern-owner",
                question_id=first.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.REJECT),
                idempotency_key="pattern-stale-response",
            )
        with pytest.raises(QuestionNotFound):
            await repository.respond_to_question(
                owner_user_id="pattern-foreign-owner",
                question_id=replacement.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.CONFIRM),
                idempotency_key="pattern-foreign-response",
            )

        request = QuestionResponseRequest(
            kind=QuestionResponseKind.CORRECT,
            correction="Steak is common near the end of the work week.",
            explanation="Thursday is not strict when plans move.",
        )
        result = await repository.respond_to_question(
            owner_user_id="pattern-owner",
            question_id=replacement.id,
            request=request,
            idempotency_key="pattern-correct-response",
        )
        retry = await repository.respond_to_question(
            owner_user_id="pattern-owner",
            question_id=replacement.id,
            request=request,
            idempotency_key="pattern-correct-response",
        )
        assert retry == result
        assert result.feedback is None
        assert result.revision is None
        assert result.question.response_kind == QuestionResponseKind.CORRECT
        response_snapshot = await (
            client.collection("accounts")
            .document(owner.id)
            .collection("question_responses")
            .document(result.response.id)
            .get()
        )
        response_raw = response_snapshot.to_dict() or {}
        assert "idempotency_key" not in response_raw
        assert response_raw["idempotency_hash"] == result.response.id
        assert response_raw["correction"] == request.correction
        with pytest.raises(IdempotencyConflict):
            await repository.respond_to_question(
                owner_user_id="pattern-owner",
                question_id=replacement.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.REJECT),
                idempotency_key="pattern-correct-response",
            )

        competing = await repository.open_pattern_question(
            account_id=owner.id,
            prompt="Weekday breakfasts appear different from weekends. Is that accurate?",
            reason="The cited meal revisions show two candidate routines.",
            tentative_claim="Weekday and weekend breakfasts follow different routines",
            evidence=evidence,
        )
        outcomes = await asyncio.gather(
            repository.respond_to_question(
                owner_user_id="pattern-owner",
                question_id=competing.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.CONFIRM),
                idempotency_key="pattern-race-confirm",
            ),
            repository.respond_to_question(
                owner_user_id="pattern-owner",
                question_id=competing.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.REJECT),
                idempotency_key="pattern-race-reject",
            ),
            return_exceptions=True,
        )
        assert (
            len([item for item in outcomes if isinstance(item, QuestionResponseResult)]) == 1
        ), repr(outcomes)
        failures = [item for item in outcomes if isinstance(item, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], QuestionAlreadyAnswered)
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
        questions = await repository.list_questions(
            "event-publication-owner",
            question_status=QuestionStatus.OPEN,
        )
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
        assert len(questions) == 1
        assert questions[0].kind == "event_clarification"
        assert questions[0].meal_id == meal.id
        assert questions[0].event_id == event.id
        assert questions[0].choices == ["Air-fried steak", "Air-fried lamb"]
        assert questions[0].source_revision_number == 1
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
        assert first["revision_numbers"] == [1, 2, 3, 4]
        assert first["knowledge_revision_numbers"] == [1, 2]
        assert first["knowledge_lifecycle"] == "reinforced"
        assert first["proposed_knowledge_revision_numbers"] == [1, 2]
        assert first["proposed_knowledge_lifecycle"] == "confirmed"
        assert first["model_calls"] == 0
        feedback = [
            snapshot
            async for snapshot in client.collection("accounts")
            .document(SMOKE_ACCOUNT_ID)
            .collection("feedback")
            .stream()
        ]
        assert len(feedback) == 3
        client.close()

    asyncio.run(scenario())
