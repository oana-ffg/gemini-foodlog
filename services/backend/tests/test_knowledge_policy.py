from datetime import UTC, datetime, timedelta

from foodlog_backend.knowledge_policy import (
    KnowledgeCandidate,
    KnowledgeClaim,
    KnowledgeEvidenceClass,
    constrain_generalization,
    resolve_knowledge,
)
from foodlog_backend.models import KnowledgeBeliefStrength, KnowledgeLifecycle

NOW = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
AIR_FRYER = {"air fryer", "basket by sink"}


def candidate(
    identity: str,
    value: str,
    evidence_class: KnowledgeEvidenceClass,
    *,
    conditions: tuple[str, ...] = ("air fryer", "basket by sink"),
    lifecycle: KnowledgeLifecycle = KnowledgeLifecycle.INFERRED,
    observed_at: datetime = NOW,
    expires_at: datetime | None = None,
    repetition_count: int = 1,
) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        id=identity,
        claim=KnowledgeClaim(
            dimension="Likely meal",
            value=value,
            conditions=conditions,
        ),
        evidence_class=evidence_class,
        lifecycle=lifecycle,
        belief_strength=KnowledgeBeliefStrength.MODERATE,
        observed_at=observed_at,
        expires_at=expires_at,
        repetition_count=repetition_count,
    )


def test_current_explicit_correction_outranks_confirmed_and_inferred_history() -> None:
    resolution = resolve_knowledge(
        [
            candidate(
                "confirmed-steak",
                "steak",
                KnowledgeEvidenceClass.CONFIRMED_HOUSEHOLD_KNOWLEDGE,
                lifecycle=KnowledgeLifecycle.CONFIRMED,
            ),
            candidate(
                "repeated-steak",
                "steak",
                KnowledgeEvidenceClass.REPEATED_CONFIRMED_OUTCOME,
                lifecycle=KnowledgeLifecycle.REINFORCED,
                repetition_count=4,
            ),
            candidate(
                "current-chicken-correction",
                "chicken",
                KnowledgeEvidenceClass.CURRENT_USER_CORRECTION,
                lifecycle=KnowledgeLifecycle.CONFIRMED,
            ),
        ],
        dimension="likely meal",
        event_conditions=AIR_FRYER,
        as_of=NOW,
    )

    assert resolution.primary_candidate_id == "current-chicken-correction"
    assert resolution.alternative_candidate_ids == (
        "confirmed-steak",
        "repeated-steak",
    )
    assert resolution.requires_clarification is False


def test_recent_unusual_purchase_preserves_best_guess_but_forces_uncertainty() -> None:
    resolution = resolve_knowledge(
        [
            candidate(
                "usual-steak",
                "steak",
                KnowledgeEvidenceClass.CONFIRMED_HOUSEHOLD_KNOWLEDGE,
                lifecycle=KnowledgeLifecycle.CONFIRMED,
                observed_at=NOW - timedelta(days=60),
            ),
            candidate(
                "new-lamb-purchase",
                "lamb",
                KnowledgeEvidenceClass.RECENT_PURCHASE,
                observed_at=NOW - timedelta(days=1),
            ),
        ],
        dimension="likely meal",
        event_conditions=AIR_FRYER,
        as_of=NOW,
    )

    assert resolution.primary_candidate_id == "usual-steak"
    assert resolution.alternative_candidate_ids == ("new-lamb-purchase",)
    assert resolution.requires_clarification is True


def test_narrow_confirmed_exception_beats_equally_confirmed_broad_pattern() -> None:
    resolution = resolve_knowledge(
        [
            candidate(
                "usual-steak",
                "steak",
                KnowledgeEvidenceClass.CONFIRMED_HOUSEHOLD_KNOWLEDGE,
                conditions=("air fryer",),
                lifecycle=KnowledgeLifecycle.CONFIRMED,
            ),
            candidate(
                "visitor-duck",
                "duck",
                KnowledgeEvidenceClass.CONFIRMED_HOUSEHOLD_KNOWLEDGE,
                conditions=("air fryer", "visitor meal"),
                lifecycle=KnowledgeLifecycle.CONFIRMED,
            ),
        ],
        dimension="likely meal",
        event_conditions={"air fryer", "visitor meal"},
        as_of=NOW,
    )

    assert resolution.primary_candidate_id == "visitor-duck"
    assert resolution.requires_clarification is False


def test_retired_or_expired_context_cannot_override_active_knowledge() -> None:
    retired = candidate(
        "retired-duck",
        "duck",
        KnowledgeEvidenceClass.CURRENT_USER_INSTRUCTION,
        lifecycle=KnowledgeLifecycle.RETIRED,
    )
    expired = candidate(
        "expired-lamb",
        "lamb",
        KnowledgeEvidenceClass.CURRENT_USER_INSTRUCTION,
        observed_at=NOW - timedelta(days=1),
        expires_at=NOW - timedelta(seconds=1),
    )
    resolution = resolve_knowledge(
        [
            candidate(
                "usual-steak",
                "steak",
                KnowledgeEvidenceClass.CONFIRMED_HOUSEHOLD_KNOWLEDGE,
                lifecycle=KnowledgeLifecycle.CONFIRMED,
            ),
            retired,
            expired,
        ],
        dimension="likely meal",
        event_conditions=AIR_FRYER,
        as_of=NOW,
    )

    assert resolution.primary_candidate_id == "usual-steak"
    assert resolution.alternative_candidate_ids == ()
    assert resolution.requires_clarification is False


def test_generalization_cannot_borrow_confirmation_beyond_user_words() -> None:
    user_statement = KnowledgeClaim(
        dimension="likely meal",
        value="steak",
        conditions=("air fryer", "thursday"),
    )

    exact = constrain_generalization(
        proposed_claim=user_statement,
        user_confirmed_claims=[user_statement],
    )
    narrower = constrain_generalization(
        proposed_claim=KnowledgeClaim(
            dimension="likely meal",
            value="steak",
            conditions=("air fryer", "thursday", "red meat visible"),
        ),
        user_confirmed_claims=[user_statement],
    )
    broader = constrain_generalization(
        proposed_claim=KnowledgeClaim(
            dimension="likely meal",
            value="steak",
            conditions=("air fryer",),
        ),
        user_confirmed_claims=[user_statement],
    )
    changed_value = constrain_generalization(
        proposed_claim=KnowledgeClaim(
            dimension="likely meal",
            value="red meat",
            conditions=("air fryer", "thursday"),
        ),
        user_confirmed_claims=[user_statement],
    )

    assert exact.inherits_user_confirmation is True
    assert narrower.inherits_user_confirmation is True
    assert exact.lifecycle == KnowledgeLifecycle.CONFIRMED
    assert broader.inherits_user_confirmation is False
    assert broader.lifecycle == KnowledgeLifecycle.INFERRED
    assert changed_value.inherits_user_confirmation is False


def test_claim_scope_normalization_is_deterministic() -> None:
    claim = KnowledgeClaim(
        dimension="  LIKELY  MEAL ",
        value=" Steak ",
        conditions=("Basket  By Sink", "\uff21\uff29\uff32 fryer", "basket by sink"),
    )

    assert claim.dimension == "likely meal"
    assert claim.value == "steak"
    assert claim.conditions == ("air fryer", "basket by sink")
