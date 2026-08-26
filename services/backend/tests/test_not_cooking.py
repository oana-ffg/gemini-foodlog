import asyncio
from hashlib import sha256

import pytest
from pydantic import ValidationError

from foodlog_backend.errors import (
    CrossAccountAccess,
    IdempotencyConflict,
    InvalidMealFeedbackTransition,
)
from foodlog_backend.feedback_learning import (
    FeedbackLearningOutcome,
    FeedbackLearningService,
)
from foodlog_backend.models import (
    Confidence,
    MealEntry,
    MealFeedbackKind,
    MealFeedbackRequest,
    MealStatus,
    QuestionStatus,
)
from foodlog_backend.repository import InMemoryRepository


async def _saved_meal(repository: InMemoryRepository, owner_user_id: str) -> MealEntry:
    account = await repository.provision_account(owner_user_id)
    camera = await repository.create_browser_camera(
        owner_user_id,
        "Kitchen camera",
        f"browser-{owner_user_id}",
    )
    capture, _, _ = await repository.reserve_capture(
        capture_id=f"capture-{owner_user_id}",
        account=account,
        camera=camera,
        idempotency_key=f"capture-{owner_user_id}-0001",
        content_type="image/jpeg",
        content_sha256=sha256(owner_user_id.encode()).hexdigest(),
        object_key=f"accounts/{account.id}/captures/capture-{owner_user_id}.jpg",
        metadata=None,
    )
    return await repository.save_meal(
        account_id=account.id,
        meal=MealEntry(
            id=f"meal-{owner_user_id}",
            account_id=account.id,
            capture_id=capture.id,
            title="Cat on the counter",
            confidence=Confidence.UNCERTAIN,
            components=[],
            observations=["A cat is standing beside the sink."],
            alternatives=["Kitchen activity without food preparation"],
            rationale="No ingredients or cooking action are visible.",
            clarification_question=(
                "Is this merely the cat on the counter, rather than food preparation?"
            ),
            clarification_reason="The distinction determines whether this belongs in the journal.",
        ),
    )


def test_not_cooking_request_forbids_correction_and_learning_payloads() -> None:
    with pytest.raises(ValidationError):
        MealFeedbackRequest(
            kind=MealFeedbackKind.NOT_COOKING,
            actual_meal="Steak",
        )
    with pytest.raises(ValidationError):
        MealFeedbackRequest(
            kind=MealFeedbackKind.NOT_COOKING,
            explanation="The cat jumped onto the counter.",
            learning_disposition="reusable",
        )


def test_not_cooking_is_immutable_hidden_and_exactly_retryable() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        meal = await _saved_meal(repository, "not-cooking-owner")
        question = await repository.open_question(
            account_id=meal.account_id,
            meal=meal,
            prompt="Was this the cat or food preparation?",
            reason="Only the first case should be excluded from the journal.",
        )
        request = MealFeedbackRequest(
            kind=MealFeedbackKind.NOT_COOKING,
            explanation="The cat jumped onto the counter; nobody was preparing food.",
        )
        service = FeedbackLearningService(repository)

        first = await service.record(
            owner_user_id="not-cooking-owner",
            meal_id=meal.id,
            request=request,
            idempotency_key="not-cooking-feedback-0001",
        )
        retry = await service.record(
            owner_user_id="not-cooking-owner",
            meal_id=meal.id,
            request=request,
            idempotency_key="not-cooking-feedback-0001",
        )

        assert retry == first
        assert first.learning_outcome == FeedbackLearningOutcome.NOT_COOKING
        assert first.knowledge is None
        assert first.feedback.explanation == request.explanation
        assert first.revision.status == MealStatus.NOT_COOKING
        assert first.revision.inference.title == meal.title
        assert await repository.list_meals("not-cooking-owner") == []
        retained = await repository.meal_for_owner("not-cooking-owner", meal.id)
        assert retained.status == MealStatus.NOT_COOKING
        revisions = await repository.list_meal_revisions("not-cooking-owner", meal.id)
        assert [revision.status for revision in revisions] == [
            MealStatus.PROVISIONAL,
            MealStatus.NOT_COOKING,
        ]
        assert revisions[0].inference == revisions[1].inference
        assert await repository.list_questions(
            "not-cooking-owner",
            question_status=QuestionStatus.OPEN,
        ) == []
        superseded = await repository.list_questions(
            "not-cooking-owner",
            question_status=QuestionStatus.SUPERSEDED,
        )
        assert [item.id for item in superseded] == [question.id]

        with pytest.raises(IdempotencyConflict):
            await service.record(
                owner_user_id="not-cooking-owner",
                meal_id=meal.id,
                request=request.model_copy(update={"explanation": "Different reason."}),
                idempotency_key="not-cooking-feedback-0001",
            )
        with pytest.raises(InvalidMealFeedbackTransition):
            await service.record(
                owner_user_id="not-cooking-owner",
                meal_id=meal.id,
                request=request,
                idempotency_key="not-cooking-feedback-0002",
            )
        with pytest.raises(InvalidMealFeedbackTransition):
            await service.record(
                owner_user_id="not-cooking-owner",
                meal_id=meal.id,
                request=MealFeedbackRequest(kind=MealFeedbackKind.CONFIRM),
                idempotency_key="not-cooking-confirm-0001",
            )

    asyncio.run(scenario())


def test_explicit_correction_reclassifies_not_cooking_without_erasing_history() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        meal = await _saved_meal(repository, "reclassify-owner")
        service = FeedbackLearningService(repository)
        await service.record(
            owner_user_id="reclassify-owner",
            meal_id=meal.id,
            request=MealFeedbackRequest(kind=MealFeedbackKind.NOT_COOKING),
            idempotency_key="reclassify-discard-0001",
        )

        with pytest.raises(InvalidMealFeedbackTransition):
            await service.record(
                owner_user_id="reclassify-owner",
                meal_id=meal.id,
                request=MealFeedbackRequest(kind=MealFeedbackKind.CORRECT),
                idempotency_key="reclassify-empty-0001",
            )

        reclassified = await service.record(
            owner_user_id="reclassify-owner",
            meal_id=meal.id,
            request=MealFeedbackRequest(
                kind=MealFeedbackKind.CORRECT,
                actual_meal="Steak",
                explanation="I discarded the wrong event; this was food preparation.",
            ),
            idempotency_key="reclassify-steak-0001",
        )

        assert reclassified.revision.status == MealStatus.CORRECTED
        assert reclassified.revision.inference.title == "Steak"
        assert [item.id for item in await repository.list_meals("reclassify-owner")] == [
            meal.id
        ]
        revisions = await repository.list_meal_revisions("reclassify-owner", meal.id)
        assert [revision.status for revision in revisions] == [
            MealStatus.PROVISIONAL,
            MealStatus.NOT_COOKING,
            MealStatus.CORRECTED,
        ]

    asyncio.run(scenario())


def test_not_cooking_cannot_cross_account_boundaries() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        meal = await _saved_meal(repository, "not-cooking-owner-a")
        await repository.provision_account("not-cooking-owner-b")

        with pytest.raises(CrossAccountAccess):
            await repository.record_meal_feedback(
                owner_user_id="not-cooking-owner-b",
                meal_id=meal.id,
                request=MealFeedbackRequest(kind=MealFeedbackKind.NOT_COOKING),
                idempotency_key="not-cooking-cross-account-0001",
            )

    asyncio.run(scenario())
