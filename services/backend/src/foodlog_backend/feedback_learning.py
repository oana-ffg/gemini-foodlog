from __future__ import annotations

from enum import StrEnum
from hashlib import sha256

from pydantic import ConfigDict

from .audit import record_audit_event
from .knowledge_updates import (
    ConfirmedClaimSource,
    HouseholdKnowledgeUpdater,
    KnowledgeUpdateIntent,
    KnowledgeUpdateProposal,
)
from .models import (
    AuditAction,
    AuditActorKind,
    AuditSource,
    ComponentCorrection,
    IngredientCorrection,
    KnowledgeClaim,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeRevisionResult,
    KnowledgeRevisionSource,
    MealFeedbackKind,
    MealFeedbackLearningDisposition,
    MealFeedbackRequest,
    MealFeedbackResult,
    PreparationMethodCorrection,
    WholeMealCorrection,
)
from .repository import Repository


class FeedbackLearningOutcome(StrEnum):
    CONFIRMATION_ONLY = "confirmation_only"
    NOT_COOKING = "not_cooking"
    WRONG_ONLY = "wrong_only"
    MEAL_ONLY = "meal_only"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    UNCLASSIFIED_EXPLANATION = "unclassified_explanation"
    KNOWLEDGE_APPLIED = "knowledge_applied"


class MealFeedbackLearningResult(MealFeedbackResult):
    model_config = ConfigDict(extra="forbid")

    learning_outcome: FeedbackLearningOutcome
    knowledge: KnowledgeRevisionResult | None = None


class FeedbackLearningService:
    """Persist feedback first, then apply only explicitly reusable exact guidance."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository
        self._updater = HouseholdKnowledgeUpdater(repository)

    async def record(
        self,
        *,
        owner_user_id: str,
        meal_id: str,
        request: MealFeedbackRequest,
        idempotency_key: str,
    ) -> MealFeedbackLearningResult:
        feedback = await self._repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal_id,
            request=request,
            idempotency_key=idempotency_key,
        )
        await record_audit_event(
            self._repository,
            account_id=feedback.feedback.account_id,
            action=AuditAction.MEAL_FEEDBACK_RECORDED,
            actor_kind=AuditActorKind.USER,
            source=AuditSource.API,
            subject_kind="feedback",
            subject_id=feedback.feedback.id,
        )
        outcome = self._outcome_without_learning(request)
        if outcome is not None:
            return MealFeedbackLearningResult(
                feedback=feedback.feedback,
                revision=feedback.revision,
                learning_outcome=outcome,
            )

        corrected_value, dimension, scope = _corrected_knowledge_target(request)
        explanation = request.explanation
        if explanation is None:  # pragma: no cover - guarded by request validation
            raise ValueError("reusable feedback requires an explanation")
        claim = KnowledgeClaim(
            dimension=dimension,
            value=corrected_value,
            conditions=(f"correction scope: {scope}",),
        )
        feedback_evidence = KnowledgeEvidenceReference(
            kind=KnowledgeEvidenceKind.FEEDBACK,
            id=feedback.feedback.id,
            role=KnowledgeEvidenceRole.SUPPORTS,
        )
        topic_key = _topic_key(dimension=dimension, value=corrected_value)
        knowledge = await self._updater.apply_current(
            account_id=feedback.feedback.account_id,
            proposal=KnowledgeUpdateProposal(
                topic_key=topic_key,
                title=_bounded_title(corrected_value),
                statement=explanation,
                claim=claim,
                intent=KnowledgeUpdateIntent.CONFIRM,
                source=KnowledgeRevisionSource.USER_FEEDBACK,
                evidence=(
                    feedback_evidence,
                    KnowledgeEvidenceReference(
                        kind=KnowledgeEvidenceKind.MEAL_REVISION,
                        id=feedback.revision.id,
                        role=KnowledgeEvidenceRole.CONTEXT,
                    ),
                ),
                confirmed_sources=(
                    ConfirmedClaimSource(claim=claim, evidence=feedback_evidence),
                ),
                reason=(
                    "The user explicitly marked this correction explanation as reusable "
                    "future identification guidance."
                ),
            ),
            idempotency_key=f"feedback-learning-v1:{feedback.feedback.id}",
        )
        return MealFeedbackLearningResult(
            feedback=feedback.feedback,
            revision=feedback.revision,
            learning_outcome=FeedbackLearningOutcome.KNOWLEDGE_APPLIED,
            knowledge=knowledge,
        )

    @staticmethod
    def _outcome_without_learning(
        request: MealFeedbackRequest,
    ) -> FeedbackLearningOutcome | None:
        if request.kind == MealFeedbackKind.CONFIRM:
            return FeedbackLearningOutcome.CONFIRMATION_ONLY
        if request.kind == MealFeedbackKind.NOT_COOKING:
            return FeedbackLearningOutcome.NOT_COOKING
        has_replacement = request.actual_meal is not None or request.correction is not None
        if request.learning_disposition == (
            MealFeedbackLearningDisposition.INSUFFICIENT_INFORMATION
        ):
            return FeedbackLearningOutcome.INSUFFICIENT_INFORMATION
        if not has_replacement:
            return FeedbackLearningOutcome.WRONG_ONLY
        if request.explanation is None:
            return FeedbackLearningOutcome.MEAL_ONLY
        if request.learning_disposition != MealFeedbackLearningDisposition.REUSABLE:
            return FeedbackLearningOutcome.UNCLASSIFIED_EXPLANATION
        return None


def _corrected_knowledge_target(
    request: MealFeedbackRequest,
) -> tuple[str, str, str]:
    if request.actual_meal is not None:
        return request.actual_meal, "meal identification guidance", "meal"
    correction = request.correction
    if isinstance(correction, WholeMealCorrection):
        return correction.title, "meal identification guidance", correction.scope
    if isinstance(correction, ComponentCorrection):
        return (
            correction.replacement.name,
            "component identification guidance",
            correction.scope,
        )
    if isinstance(correction, IngredientCorrection):
        return correction.replacement, "ingredient identification guidance", correction.scope
    if isinstance(correction, PreparationMethodCorrection):
        return (
            correction.replacement,
            "preparation method identification guidance",
            correction.scope,
        )
    raise ValueError("reusable feedback requires a corrected meal or target")


def _topic_key(*, dimension: str, value: str) -> str:
    normalized_value = " ".join(value.casefold().split())
    digest = sha256(f"{dimension}\0{normalized_value}".encode()).hexdigest()[:16]
    return f"{dimension}: {normalized_value[:100]} [{digest}]"


def _bounded_title(value: str) -> str:
    suffix = " identification guidance"
    return f"{value[: 200 - len(suffix)]}{suffix}"
