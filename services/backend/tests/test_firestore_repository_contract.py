from __future__ import annotations

import asyncio
import os

import pytest
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.errors import QuestionAlreadyAnswered
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    Confidence,
    MealComponent,
    MealEntry,
    MealFeedbackKind,
    MealFeedbackRequest,
    MealStatus,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionStatus,
)


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
        assert len(successes) == 1
        assert len(failures) == 1
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
        client.close()

    asyncio.run(scenario())
