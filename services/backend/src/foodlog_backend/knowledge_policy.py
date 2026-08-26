from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from unicodedata import normalize as unicode_normalize

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import KnowledgeBeliefStrength, KnowledgeLifecycle, utc_now


def _normalize_term(value: str, *, label: str, max_length: int = 200) -> str:
    normalized = " ".join(unicode_normalize("NFKC", value).casefold().split())
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{label} must contain 1-{max_length} normalized characters")
    return normalized


class KnowledgeEvidenceClass(StrEnum):
    CURRENT_USER_CORRECTION = "current_user_correction"
    CURRENT_USER_INSTRUCTION = "current_user_instruction"
    CONFIRMED_HOUSEHOLD_KNOWLEDGE = "confirmed_household_knowledge"
    REPEATED_CONFIRMED_OUTCOME = "repeated_confirmed_outcome"
    REPEATED_INFERRED_OBSERVATION = "repeated_inferred_observation"
    RECENT_PURCHASE = "recent_purchase"
    ONE_OFF_INFERENCE = "one_off_inference"


class KnowledgeClaim(BaseModel):
    """A deterministic claim plus the exact conditions in which it applies.

    Fewer conditions mean broader applicability. The policy deliberately does no
    semantic paraphrase matching: the agent must preserve the same normalized
    dimension/value when it asks to inherit a user's confirmation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=200)
    conditions: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("dimension")
    @classmethod
    def normalize_dimension(cls, value: str) -> str:
        return _normalize_term(value, label="claim dimension", max_length=120)

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        return _normalize_term(value, label="claim value")

    @field_validator("conditions")
    @classmethod
    def normalize_conditions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            sorted({_normalize_term(value, label="claim condition") for value in values})
        )
        return normalized

    def applies_to(self, event_conditions: set[str]) -> bool:
        normalized_event = {
            _normalize_term(value, label="event condition") for value in event_conditions
        }
        return set(self.conditions).issubset(normalized_event)

    def is_no_broader_than(self, source: KnowledgeClaim) -> bool:
        return (
            self.dimension == source.dimension
            and self.value == source.value
            and set(self.conditions).issuperset(source.conditions)
        )


class KnowledgeCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    claim: KnowledgeClaim
    evidence_class: KnowledgeEvidenceClass
    lifecycle: KnowledgeLifecycle
    belief_strength: KnowledgeBeliefStrength
    observed_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    repetition_count: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_repetition_and_expiry(self) -> KnowledgeCandidate:
        repeated = {
            KnowledgeEvidenceClass.REPEATED_CONFIRMED_OUTCOME,
            KnowledgeEvidenceClass.REPEATED_INFERRED_OBSERVATION,
        }
        if self.evidence_class in repeated and self.repetition_count < 2:
            raise ValueError("repeated evidence classes require at least two observations")
        if self.expires_at is not None and self.expires_at <= self.observed_at:
            raise ValueError("knowledge candidate expiry must follow its observation")
        return self

    def is_active(self, *, as_of: datetime) -> bool:
        return self.lifecycle not in {
            KnowledgeLifecycle.CONTRADICTED,
            KnowledgeLifecycle.RETIRED,
        } and (self.expires_at is None or self.expires_at > as_of)


class KnowledgeResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    primary_candidate_id: str | None
    alternative_candidate_ids: tuple[str, ...] = ()
    requires_clarification: bool = False
    reasons: tuple[str, ...] = ()


class ScopedGeneralizationDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lifecycle: KnowledgeLifecycle
    belief_strength: KnowledgeBeliefStrength
    inherits_user_confirmation: bool
    reason: str


_PRECEDENCE = {
    KnowledgeEvidenceClass.CURRENT_USER_CORRECTION: 700,
    KnowledgeEvidenceClass.CURRENT_USER_INSTRUCTION: 600,
    KnowledgeEvidenceClass.CONFIRMED_HOUSEHOLD_KNOWLEDGE: 500,
    KnowledgeEvidenceClass.REPEATED_CONFIRMED_OUTCOME: 400,
    KnowledgeEvidenceClass.REPEATED_INFERRED_OBSERVATION: 300,
    KnowledgeEvidenceClass.RECENT_PURCHASE: 200,
    KnowledgeEvidenceClass.ONE_OFF_INFERENCE: 100,
}


def resolve_knowledge(
    candidates: list[KnowledgeCandidate],
    *,
    dimension: str,
    event_conditions: set[str],
    as_of: datetime | None = None,
    recent_conflict_window: timedelta = timedelta(days=30),
) -> KnowledgeResolution:
    """Select the best applicable claim and preserve material recent conflict.

    Precedence decides first; a more specific claim wins only among otherwise equal
    evidence classes. A recent lower-precedence conflict cannot silently replace the
    best claim, but it does force the caller to surface uncertainty unless a current
    explicit correction or instruction already settles the event.
    """

    normalized_dimension = _normalize_term(dimension, label="resolution dimension", max_length=120)
    current_time = as_of or utc_now()
    applicable = [
        candidate
        for candidate in candidates
        if candidate.claim.dimension == normalized_dimension
        and candidate.is_active(as_of=current_time)
        and candidate.claim.applies_to(event_conditions)
    ]
    if not applicable:
        return KnowledgeResolution(
            primary_candidate_id=None,
            reasons=("No active claim applies to this event scope.",),
        )

    ordered = sorted(
        applicable,
        key=lambda candidate: (
            _PRECEDENCE[candidate.evidence_class],
            len(candidate.claim.conditions),
            candidate.observed_at,
            candidate.id,
        ),
        reverse=True,
    )
    primary = ordered[0]
    alternatives = tuple(
        candidate.id for candidate in ordered[1:] if candidate.claim.value != primary.claim.value
    )
    if not alternatives:
        return KnowledgeResolution(
            primary_candidate_id=primary.id,
            reasons=("The highest-precedence applicable evidence is unopposed.",),
        )

    settling_classes = {
        KnowledgeEvidenceClass.CURRENT_USER_CORRECTION,
        KnowledgeEvidenceClass.CURRENT_USER_INSTRUCTION,
    }
    tied_at_top = any(
        _PRECEDENCE[candidate.evidence_class] == _PRECEDENCE[primary.evidence_class]
        and len(candidate.claim.conditions) == len(primary.claim.conditions)
        and candidate.claim.value != primary.claim.value
        for candidate in ordered[1:]
    )
    recent_conflict = any(
        current_time - candidate.observed_at <= recent_conflict_window
        for candidate in ordered[1:]
        if candidate.claim.value != primary.claim.value
        and (
            _PRECEDENCE[candidate.evidence_class] < _PRECEDENCE[primary.evidence_class]
            or len(candidate.claim.conditions) >= len(primary.claim.conditions)
        )
    )
    requires_clarification = tied_at_top or (
        primary.evidence_class not in settling_classes and recent_conflict
    )
    reason = (
        "Conflicting evidence remains material; keep the best guess but ask a focused question."
        if requires_clarification
        else "A current explicit user statement settles the conflicting weaker evidence."
    )
    return KnowledgeResolution(
        primary_candidate_id=primary.id,
        alternative_candidate_ids=alternatives,
        requires_clarification=requires_clarification,
        reasons=(reason,),
    )


def constrain_generalization(
    *,
    proposed_claim: KnowledgeClaim,
    user_confirmed_claims: list[KnowledgeClaim],
) -> ScopedGeneralizationDecision:
    """Prevent a derived claim from borrowing confirmation beyond the user's words."""

    supporting_claim = next(
        (claim for claim in user_confirmed_claims if proposed_claim.is_no_broader_than(claim)),
        None,
    )
    if supporting_claim is not None:
        return ScopedGeneralizationDecision(
            lifecycle=KnowledgeLifecycle.CONFIRMED,
            belief_strength=KnowledgeBeliefStrength.STRONG,
            inherits_user_confirmation=True,
            reason=(
                "The proposed claim preserves the confirmed dimension and value and "
                "is no broader than the user's stated conditions."
            ),
        )
    return ScopedGeneralizationDecision(
        lifecycle=KnowledgeLifecycle.INFERRED,
        belief_strength=KnowledgeBeliefStrength.WEAK,
        inherits_user_confirmation=False,
        reason=(
            "The proposed claim changes or broadens the user's exact scope, so the "
            "extension remains an inference."
        ),
    )
