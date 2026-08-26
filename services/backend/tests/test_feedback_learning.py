import asyncio
from hashlib import sha256

from foodlog_backend.feedback_learning import (
    FeedbackLearningOutcome,
    FeedbackLearningService,
)
from foodlog_backend.models import (
    Confidence,
    KnowledgeLifecycle,
    MealEntry,
    MealFeedbackKind,
    MealFeedbackLearningDisposition,
    MealFeedbackRequest,
)
from foodlog_backend.repository import InMemoryRepository


async def saved_meal(
    repository: InMemoryRepository,
    *,
    account,
    owner_user_id: str,
    identity: str,
) -> MealEntry:
    camera = await repository.create_browser_camera(
        owner_user_id,
        f"Camera {identity}",
        f"browser-{identity}",
    )
    capture_id = f"capture-{identity}"
    capture, _, _ = await repository.reserve_capture(
        capture_id=capture_id,
        account=account,
        camera=camera,
        idempotency_key=f"capture-idempotency-{identity}",
        content_type="image/jpeg",
        content_sha256=sha256(identity.encode()).hexdigest(),
        object_key=f"accounts/{account.id}/captures/{capture_id}.jpg",
        metadata=None,
    )
    return await repository.save_meal(
        account_id=account.id,
        meal=MealEntry(
            id=identity,
            account_id=account.id,
            capture_id=capture.id,
            title="Tentative red meat",
            confidence=Confidence.UNCERTAIN,
            components=[],
            observations=["Red meat is barely visible near the air-fryer basket."],
            alternatives=["Steak", "Lamb"],
            rationale="The distant view does not reveal the cut.",
        ),
    )


def test_four_feedback_modes_only_learn_from_explicitly_reusable_explanation() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("feedback-learning-owner")
        service = FeedbackLearningService(repository)
        requests = [
            (
                "wrong-only",
                MealFeedbackRequest(kind=MealFeedbackKind.CORRECT),
                FeedbackLearningOutcome.WRONG_ONLY,
            ),
            (
                "meal-only",
                MealFeedbackRequest(
                    kind=MealFeedbackKind.CORRECT,
                    actual_meal="Steak",
                ),
                FeedbackLearningOutcome.MEAL_ONLY,
            ),
            (
                "insufficient",
                MealFeedbackRequest(
                    kind=MealFeedbackKind.CORRECT,
                    actual_meal="Steak",
                    explanation="There was no visible cue that could distinguish the cut.",
                    learning_disposition=(
                        MealFeedbackLearningDisposition.INSUFFICIENT_INFORMATION
                    ),
                ),
                FeedbackLearningOutcome.INSUFFICIENT_INFORMATION,
            ),
            (
                "reusable",
                MealFeedbackRequest(
                    kind=MealFeedbackKind.CORRECT,
                    actual_meal="Steak",
                    explanation="The dark green beef label means this package is steak.",
                    learning_disposition=MealFeedbackLearningDisposition.REUSABLE,
                ),
                FeedbackLearningOutcome.KNOWLEDGE_APPLIED,
            ),
        ]

        results = []
        for identity, request, expected in requests:
            meal = await saved_meal(
                repository,
                account=account,
                owner_user_id="feedback-learning-owner",
                identity=f"feedback-learning-{identity}",
            )
            result = await service.record(
                owner_user_id="feedback-learning-owner",
                meal_id=meal.id,
                request=request,
                idempotency_key=f"feedback-learning-{identity}-0001",
            )
            assert result.learning_outcome == expected
            results.append(result)

        assert all(result.knowledge is None for result in results[:3])
        learned = results[3].knowledge
        assert learned is not None
        assert learned.revision.lifecycle == KnowledgeLifecycle.CONFIRMED
        assert learned.revision.statement == requests[3][1].explanation
        assert learned.revision.evidence[0].id == results[3].feedback.id

    asyncio.run(scenario())


def test_reusable_feedback_retry_and_later_same_topic_append_exactly_once() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("feedback-retry-owner")
        service = FeedbackLearningService(repository)
        request = MealFeedbackRequest(
            kind=MealFeedbackKind.CORRECT,
            actual_meal="Steak",
            explanation="The dark green beef label means this package is steak.",
            learning_disposition=MealFeedbackLearningDisposition.REUSABLE,
        )
        first_meal = await saved_meal(
            repository,
            account=account,
            owner_user_id="feedback-retry-owner",
            identity="feedback-retry-first",
        )
        first = await service.record(
            owner_user_id="feedback-retry-owner",
            meal_id=first_meal.id,
            request=request,
            idempotency_key="feedback-retry-first-0001",
        )
        retry = await service.record(
            owner_user_id="feedback-retry-owner",
            meal_id=first_meal.id,
            request=request,
            idempotency_key="feedback-retry-first-0001",
        )
        assert retry == first

        second_meal = await saved_meal(
            repository,
            account=account,
            owner_user_id="feedback-retry-owner",
            identity="feedback-retry-second",
        )
        second = await service.record(
            owner_user_id="feedback-retry-owner",
            meal_id=second_meal.id,
            request=request,
            idempotency_key="feedback-retry-second-0001",
        )
        assert first.knowledge is not None
        assert second.knowledge is not None
        assert first.knowledge.page.id == second.knowledge.page.id
        assert second.knowledge.revision.number == 2
        revisions = await repository.list_knowledge_revisions(
            "feedback-retry-owner",
            second.knowledge.page.id,
        )
        assert [revision.number for revision in revisions] == [1, 2]
        assert revisions[1].previous_revision_id == revisions[0].id

    asyncio.run(scenario())


def test_unclassified_explanation_is_preserved_without_automatic_learning() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("feedback-unclassified-owner")
        meal = await saved_meal(
            repository,
            account=account,
            owner_user_id="feedback-unclassified-owner",
            identity="feedback-unclassified",
        )
        result = await FeedbackLearningService(repository).record(
            owner_user_id="feedback-unclassified-owner",
            meal_id=meal.id,
            request=MealFeedbackRequest(
                kind=MealFeedbackKind.CORRECT,
                actual_meal="Steak",
                explanation="I know what I bought, but did not describe a reusable cue.",
            ),
            idempotency_key="feedback-unclassified-0001",
        )

        assert result.learning_outcome == FeedbackLearningOutcome.UNCLASSIFIED_EXPLANATION
        assert result.knowledge is None
        assert result.feedback.explanation is not None

    asyncio.run(scenario())
