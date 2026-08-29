from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from foodlog_backend.models import PurchaseEvidenceOrigin
from foodlog_backend.repository import InMemoryRepository
from scripts.prepare_synthetic_grocery_evaluation import (
    _contains_unqualified_claim,
    load_dataset,
    scenario_client_version,
    scenario_idempotency_key,
    seed_synthetic_purchases,
    validate_activity,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures"
MANIFEST = FIXTURE_ROOT / "synthetic-grocery-evaluation.v1.json"


def test_synthetic_grocery_manifest_is_hash_locked_and_longitudinal() -> None:
    spec = load_dataset(MANIFEST, FIXTURE_ROOT)

    assert spec.dataset_id == "synthetic-grocery-longitudinal-v1"
    assert len(spec.orders) == len(spec.scenarios) == 4
    assert (spec.scenarios[-1].captured_at - spec.scenarios[0].captured_at).days == 21
    assert all(
        scenario.expected_purchase_key == order.key
        for order, scenario in zip(spec.orders, spec.scenarios, strict=True)
    )
    assert [scenario.attempt for scenario in spec.scenarios] == [1, 2, 1, 1]
    assert scenario_client_version(spec.dataset_id, spec.scenarios[0]) == spec.dataset_id
    assert scenario_client_version(spec.dataset_id, spec.scenarios[1]).endswith("-retry-2")
    assert scenario_idempotency_key(spec.dataset_id, spec.scenarios[0]).endswith("-v1")
    assert scenario_idempotency_key(spec.dataset_id, spec.scenarios[1]).endswith("-retry-2")


def test_synthetic_grocery_seed_is_exactly_idempotent_and_temporally_bounded() -> None:
    async def scenario() -> None:
        spec = load_dataset(MANIFEST, FIXTURE_ROOT)
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("synthetic-grocery-owner")

        first = await seed_synthetic_purchases(
            repository,
            account_id=account.id,
            spec=spec,
        )
        retry = await seed_synthetic_purchases(
            repository,
            account_id=account.id,
            spec=spec,
        )

        assert retry == first
        assert len(first) == 4
        assert len(repository._purchases) == 4
        assert len(repository._purchase_documents) == 8
        assert all(
            purchase.evidence_origin == PurchaseEvidenceOrigin.SYNTHETIC_EVALUATION
            for purchase in repository._purchases.values()
        )
        assert not repository._published_raw_mail
        assert not repository._raw_mail_authentication

        for ordinal, evaluation in enumerate(spec.scenarios, start=1):
            bundles = await repository.recent_purchase_evidence_for_account(
                account_id=account.id,
                as_of=evaluation.captured_at,
                limit=5,
            )
            visible_ids = {bundle.purchase.id for bundle in bundles}
            assert first[evaluation.expected_purchase_key] in visible_ids
            assert len(visible_ids) == ordinal

        week_one = await repository.purchase_evidence_for_owner(
            "synthetic-grocery-owner",
            first["week-one-steak"],
        )
        week_two = await repository.purchase_evidence_for_owner(
            "synthetic-grocery-owner",
            first["week-two-duck-substitution"],
        )
        week_three = await repository.purchase_evidence_for_owner(
            "synthetic-grocery-owner",
            first["week-three-removed-pork"],
        )
        week_four_at_event = next(
            bundle
            for bundle in await repository.recent_purchase_evidence_for_account(
                account_id=account.id,
                as_of=spec.scenarios[-1].captured_at,
                limit=5,
            )
            if bundle.purchase.id == first["week-four-ordered-chicken-only"]
        )
        assert week_one.reconciliation is not None
        assert week_one.reconciliation.unresolved_item_count == 1
        assert week_two.reconciliation is not None
        assert week_two.reconciliation.has_unresolved_substitution_pairing is True
        assert week_three.reconciliation is not None
        assert week_three.reconciliation.unresolved_item_count == 1
        assert week_four_at_event.purchase.latest_final_document_id is None
        assert week_four_at_event.purchase.revision_count == 1
        assert {item.disposition.value for item in week_four_at_event.items} == {"ordered"}

    asyncio.run(scenario())


def test_synthetic_grocery_activity_requires_provenance_and_rejects_future_leakage() -> None:
    spec = load_dataset(MANIFEST, FIXTURE_ROOT)
    scenario = spec.scenarios[1]
    activity = {
        "activity_hypothesis": {
            "kind": "tentative_meal",
            "confidence": "uncertain",
            "best_guess": "Duck breast",
            "alternatives": [{"label": "Chicken breast"}],
            "contextual_evidence": [
                {
                    "source_kind": "purchase",
                    "source_id": "purchase-week-two",
                    "description": "Explicitly synthetic purchase evaluation context.",
                }
            ],
            "question": {"candidate_labels": ["Duck breast", "Chicken breast"]},
        }
    }

    result = validate_activity(
        activity,
        scenario=scenario,
        expected_purchase_id="purchase-week-two",
        future_purchase_ids={"purchase-week-three"},
    )
    assert result["purchase_cited"] is True
    assert result["future_purchase_leak"] is False

    activity["activity_hypothesis"]["contextual_evidence"].append(  # type: ignore[index]
        {
            "source_kind": "purchase",
            "source_id": "purchase-week-three",
            "description": "Explicitly synthetic future context.",
        }
    )
    with pytest.raises(RuntimeError, match="from the future"):
        validate_activity(
            activity,
            scenario=scenario,
            expected_purchase_id="purchase-week-two",
            future_purchase_ids={"purchase-week-three"},
        )


def test_forbidden_purchase_claim_guard_preserves_explicit_negation() -> None:
    assert _contains_unqualified_claim(
        "the model says chicken is available",
        "chicken is available",
    )
    assert not _contains_unqualified_claim(
        "the synthetic order cannot prove chicken is available",
        "chicken is available",
    )
