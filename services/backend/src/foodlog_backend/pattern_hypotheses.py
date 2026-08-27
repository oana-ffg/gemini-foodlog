from __future__ import annotations

from .models import (
    KnowledgeBeliefStrength,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeLifecycle,
    KnowledgeRevisionDraft,
    KnowledgeRevisionSource,
    QuestionResponseKind,
    QuestionResponseRequest,
    QuestionResponseResult,
)
from .repository import Repository


class PatternHypothesisService:
    """Resolve rich pattern questions and materialize user-backed household knowledge."""

    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    async def respond(
        self,
        *,
        owner_user_id: str,
        question_id: str,
        request: QuestionResponseRequest,
        idempotency_key: str,
    ) -> QuestionResponseResult:
        result = await self._repository.respond_to_question(
            owner_user_id=owner_user_id,
            question_id=question_id,
            request=request,
            idempotency_key=idempotency_key,
        )
        question = result.question
        if question.pattern_claim is None or request.kind == QuestionResponseKind.REJECT:
            return result

        account = await self._repository.account_for_owner(owner_user_id)
        assert question.pattern_topic_key is not None
        revision_key = f"pattern-response-knowledge-v1:{result.response.id}"
        existing = await self._repository.knowledge_revision_result_for_request(
            account_id=account.id,
            idempotency_key=revision_key,
        )
        if existing is None:
            topic_key = f"pattern-claim:{question.pattern_topic_key}"
            current = await self._repository.current_knowledge_revision(
                account_id=account.id,
                topic_key=topic_key,
            )
            statement = (
                request.correction
                if request.kind == QuestionResponseKind.CORRECT
                else question.tentative_claim
            )
            assert statement is not None
            evidence = [
                KnowledgeEvidenceReference(
                    kind=KnowledgeEvidenceKind.QUESTION_RESPONSE,
                    id=result.response.id,
                    role=KnowledgeEvidenceRole.SUPPORTS,
                    note="Exact authenticated response to an evidence-backed pattern hypothesis.",
                )
            ]
            if current is not None:
                evidence.append(
                    KnowledgeEvidenceReference(
                        kind=KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                        id=current.revision.id,
                        role=KnowledgeEvidenceRole.CONTEXT,
                    )
                )
            existing = await self._repository.record_knowledge_revision(
                account_id=account.id,
                topic_key=topic_key,
                expected_revision_number=(current.revision.number if current is not None else None),
                draft=KnowledgeRevisionDraft(
                    title=" ".join(statement.split())[:200],
                    statement=statement,
                    claim=(
                        question.pattern_claim
                        if request.kind == QuestionResponseKind.CONFIRM
                        else None
                    ),
                    lifecycle=KnowledgeLifecycle.CONFIRMED,
                    belief_strength=KnowledgeBeliefStrength.STRONG,
                    source=KnowledgeRevisionSource.QUESTION_RESPONSE,
                    evidence=evidence,
                    reason=(
                        "The authenticated user confirmed this evidence-backed household pattern."
                        if request.kind == QuestionResponseKind.CONFIRM
                        else "The authenticated user supplied the exact corrected pattern; "
                        "no structured claim was inferred from their wording."
                    ),
                ),
                idempotency_key=revision_key,
            )
        return result.model_copy(update={"knowledge": existing})
