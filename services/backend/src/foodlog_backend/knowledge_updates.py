from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .knowledge_policy import constrain_generalization
from .models import (
    KnowledgeBeliefStrength,
    KnowledgeClaim,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeLifecycle,
    KnowledgeRevisionDraft,
    KnowledgeRevisionResult,
    KnowledgeRevisionSource,
)
from .repository import Repository


class KnowledgeUpdateIntent(StrEnum):
    INFER = "infer"
    REINFORCE = "reinforce"
    CONFIRM = "confirm"
    CONTRADICT = "contradict"
    RETIRE = "retire"


class ConfirmedClaimSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: KnowledgeClaim
    evidence: KnowledgeEvidenceReference

    @model_validator(mode="after")
    def require_direct_user_support(self) -> ConfirmedClaimSource:
        if self.evidence.kind not in {
            KnowledgeEvidenceKind.FEEDBACK,
            KnowledgeEvidenceKind.QUESTION_RESPONSE,
            KnowledgeEvidenceKind.USER_CONTEXT_NOTE,
        } or self.evidence.role != KnowledgeEvidenceRole.SUPPORTS:
            raise ValueError("confirmed claim scope requires direct supporting user evidence")
        return self


class KnowledgeUpdateProposal(BaseModel):
    """A bounded agent proposal; lifecycle and strength are derived server-side."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    topic_key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=2_000)
    claim: KnowledgeClaim
    intent: KnowledgeUpdateIntent
    source: KnowledgeRevisionSource
    evidence: tuple[KnowledgeEvidenceReference, ...] = Field(min_length=1, max_length=50)
    confirmed_sources: tuple[ConfirmedClaimSource, ...] = Field(default=(), max_length=20)
    reason: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_source_and_provenance(self) -> KnowledgeUpdateProposal:
        evidence_identities = {(item.kind, item.id, item.role) for item in self.evidence}
        if len(evidence_identities) != len(self.evidence):
            raise ValueError("knowledge update evidence references must be unique")
        for source in self.confirmed_sources:
            identity = (source.evidence.kind, source.evidence.id, source.evidence.role)
            if identity not in evidence_identities:
                raise ValueError("confirmed claim scope must link proposal evidence")

        user_sources = {
            KnowledgeRevisionSource.USER_FEEDBACK,
            KnowledgeRevisionSource.USER_STATEMENT,
            KnowledgeRevisionSource.QUESTION_RESPONSE,
        }
        if self.intent == KnowledgeUpdateIntent.INFER:
            if self.source != KnowledgeRevisionSource.AGENT_INFERENCE:
                raise ValueError("weak inference must use the agent inference source")
            if self.confirmed_sources:
                raise ValueError("weak inference cannot claim user-confirmed scope")
        elif self.intent in {
            KnowledgeUpdateIntent.CONFIRM,
            KnowledgeUpdateIntent.CONTRADICT,
            KnowledgeUpdateIntent.RETIRE,
        } and self.source not in user_sources:
            raise ValueError("confirmation, contradiction, and retirement require a user source")

        if self.intent == KnowledgeUpdateIntent.CONFIRM and not self.confirmed_sources:
            raise ValueError("confirmation requires exact user-confirmed claim scope")
        if self.intent == KnowledgeUpdateIntent.CONTRADICT and not any(
            item.role == KnowledgeEvidenceRole.CONTRADICTS for item in self.evidence
        ):
            raise ValueError("contradiction requires explicit contradicting evidence")
        return self


class HouseholdKnowledgeUpdater:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    async def apply(
        self,
        *,
        account_id: str,
        proposal: KnowledgeUpdateProposal,
        expected_revision_number: int | None,
        idempotency_key: str,
    ) -> KnowledgeRevisionResult:
        lifecycle, strength = self._derive_belief(proposal)
        draft = KnowledgeRevisionDraft(
            title=proposal.title,
            statement=proposal.statement,
            claim=proposal.claim,
            lifecycle=lifecycle,
            belief_strength=strength,
            source=proposal.source,
            evidence=list(proposal.evidence),
            reason=proposal.reason,
        )
        return await self._repository.record_knowledge_revision(
            account_id=account_id,
            topic_key=proposal.topic_key,
            expected_revision_number=expected_revision_number,
            draft=draft,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _derive_belief(
        proposal: KnowledgeUpdateProposal,
    ) -> tuple[KnowledgeLifecycle, KnowledgeBeliefStrength]:
        if proposal.intent == KnowledgeUpdateIntent.INFER:
            return KnowledgeLifecycle.INFERRED, KnowledgeBeliefStrength.WEAK
        if proposal.intent == KnowledgeUpdateIntent.REINFORCE:
            return KnowledgeLifecycle.REINFORCED, KnowledgeBeliefStrength.MODERATE
        if proposal.intent == KnowledgeUpdateIntent.CONTRADICT:
            return KnowledgeLifecycle.CONTRADICTED, KnowledgeBeliefStrength.WEAK
        if proposal.intent == KnowledgeUpdateIntent.RETIRE:
            return KnowledgeLifecycle.RETIRED, KnowledgeBeliefStrength.WEAK

        scoped = constrain_generalization(
            proposed_claim=proposal.claim,
            user_confirmed_claims=[source.claim for source in proposal.confirmed_sources],
        )
        return scoped.lifecycle, scoped.belief_strength
