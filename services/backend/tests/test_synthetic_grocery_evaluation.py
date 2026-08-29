from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from foodlog_backend.grouping import GroupingPolicy
from foodlog_backend.models import PurchaseEvidenceOrigin
from foodlog_backend.repository import InMemoryRepository
from scripts.prepare_synthetic_grocery_evaluation import (
    _contains_unqualified_claim,
    load_dataset,
    prior_inferred_event_count,
    scenario_capture_time,
    scenario_client_version,
    scenario_idempotency_key,
    seed_synthetic_purchases,
    selected_scenarios,
    shifted_replay_dataset,
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
    assert [scenario.attempt for scenario in spec.scenarios] == [1, 2, 3, 2]
    assert scenario_client_version(spec.dataset_id, spec.scenarios[0]) == spec.dataset_id
    assert scenario_client_version(spec.dataset_id, spec.scenarios[1]).endswith("-retry-2")
    assert scenario_idempotency_key(spec.dataset_id, spec.scenarios[0]).endswith("-v1")
    assert scenario_idempotency_key(spec.dataset_id, spec.scenarios[1]).endswith("-retry-2")
    isolated_retries = spec.scenarios[2:]
    assert all(
        scenario_capture_time(scenario) - scenario.captured_at
        > GroupingPolicy().reopen_window
        for scenario in isolated_retries
    )
    assert [number for number, _ in selected_scenarios(spec, None)] == [1, 2, 3, 4]
    assert [
        (number, scenario.key)
        for number, scenario in selected_scenarios(spec, "ambiguous-after-order-only")
    ] == [(4, "ambiguous-after-order-only")]
    with pytest.raises(ValueError, match="unknown synthetic grocery scenario"):
        selected_scenarios(spec, "does-not-exist")


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
        assert week_four_at_event.reconciliation is None
        assert {item.disposition.value for item in week_four_at_event.items} == {"ordered"}

        historical_replay = shifted_replay_dataset(
            spec,
            replay_key="history-may",
            shift_days=-56,
        )
        replay_ids = await seed_synthetic_purchases(
            repository,
            account_id=account.id,
            spec=historical_replay,
        )
        assert set(replay_ids.values()).isdisjoint(first.values())
        assert len(repository._purchases) == 8
        assert len(repository._purchase_documents) == 16
        replay_visible = await repository.recent_purchase_evidence_for_account(
            account_id=account.id,
            as_of=historical_replay.scenarios[-1].captured_at,
            limit=50,
        )
        assert {bundle.purchase.id for bundle in replay_visible} == set(replay_ids.values())

    asyncio.run(scenario())


def test_shifted_replay_is_unique_hash_bound_and_preserves_relative_time() -> None:
    source = load_dataset(MANIFEST, FIXTURE_ROOT)
    replay = shifted_replay_dataset(
        source,
        replay_key="history-may",
        shift_days=-56,
    )

    assert replay.dataset_id == f"{source.dataset_id}-history-may"
    assert "synthetic" in replay.provenance_label.casefold()
    assert "not an authenticated" in replay.provenance_label.casefold()
    assert replay.orders[0].order_reference.endswith("-history-may")
    assert replay.orders[0].final is not None
    assert replay.orders[0].final.invoice_reference.endswith("-history-may")
    assert [
        replay_order.confirmation.recorded_at - source_order.confirmation.recorded_at
        for source_order, replay_order in zip(source.orders, replay.orders, strict=True)
    ] == [replay.scenarios[0].captured_at - source.scenarios[0].captured_at] * 4
    assert all(
        replay_scenario.sha256 == source_scenario.sha256
        for source_scenario, replay_scenario in zip(
            source.scenarios,
            replay.scenarios,
            strict=True,
        )
    )

    with pytest.raises(ValueError, match="replay key"):
        shifted_replay_dataset(source, replay_key="May History", shift_days=-56)
    with pytest.raises(ValueError, match="non-zero"):
        shifted_replay_dataset(source, replay_key="history-may", shift_days=0)


def test_prior_inferred_event_count_uses_event_time_not_insertion_time() -> None:
    activities = [
        {"occurred_at": "2026-05-01T18:00:00+02:00"},
        {"occurred_at": "2026-06-01T18:00:00+02:00"},
        {"occurred_at": "2026-07-01T18:00:00+02:00"},
        {"occurred_at": None},
    ]

    assert prior_inferred_event_count(
        activities,
        as_of=datetime.fromisoformat("2026-06-15T18:00:00+02:00"),
    ) == 2

    with pytest.raises(RuntimeError, match="naive timestamp"):
        prior_inferred_event_count(
            [{"occurred_at": "2026-05-01T18:00:00"}],
            as_of=datetime.fromisoformat("2026-06-15T18:00:00+02:00"),
        )


def test_synthetic_grocery_activity_requires_provenance_and_rejects_future_leakage() -> None:
    spec = load_dataset(MANIFEST, FIXTURE_ROOT)
    scenario = spec.scenarios[1]
    activity = {
        "activity_hypothesis": {
            "kind": "tentative_meal",
            "confidence": "uncertain",
            "best_guess": "Duck breast",
            "alternatives": [{"label": "Raw poultry"}],
            "contextual_evidence": [
                {
                    "source_kind": "purchase",
                    "source_id": "purchase-week-two",
                    "description": "Explicitly synthetic purchase evaluation context.",
                }
            ],
            "question": {
                "candidate_labels": ["Raw poultry", "Duck breast"],
                "prompt": "Was this chicken breast or duck breast?",
            },
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
