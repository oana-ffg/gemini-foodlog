from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foodlog_backend.grouping import CaptureGroupingService, GroupingPolicy
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.storage import InMemoryObjectStore
from scripts.prepare_judge_dataset import (
    RealScenarioSpec,
    checked_fixture,
    load_dataset,
    propose_seeded_pattern,
    scenario_client_version,
    scenario_idempotency_key,
    selected_real_scenarios,
    validate_activity,
)
from scripts.synthetic_dataset_support import seed_synthetic_meal

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures"
MANIFEST = FIXTURE_ROOT / "judge-demo-dataset.v3.json"


def test_judge_dataset_manifest_is_complete_and_hash_locked() -> None:
    dataset = load_dataset(MANIFEST, FIXTURE_ROOT)

    assert dataset.dataset_id == "judge-demo-v1"
    assert [item.key for item in dataset.real_inference_scenarios] == [
        "red-before-learning",
        "red-after-learning",
        "ambiguous-with-context-v3",
        "cat-negative-control",
    ]
    retry = dataset.real_inference_scenarios[2]
    assert retry.attempt == 3
    assert scenario_client_version(retry) == "judge-demo-v1-retry-3"
    assert scenario_idempotency_key(dataset.dataset_id, retry) == (
        "judge-demo-v1-ambiguous-with-context-v3-retry-3"
    )
    cat_retry = dataset.real_inference_scenarios[3]
    assert cat_retry.attempt == 3
    assert scenario_client_version(cat_retry) == "judge-demo-v1-retry-3"
    assert scenario_idempotency_key(dataset.dataset_id, cat_retry) == (
        "judge-demo-v1-cat-negative-control-retry-3"
    )
    assert len(dataset.synthetic_pattern_history.events) == 6
    assert dataset.synthetic_pattern_history.expected_claim_value == "steak"
    assert all(
        event.captured_at.weekday() == 3
        for event in dataset.synthetic_pattern_history.events
    )


def test_default_scenario_preserves_original_upload_identity() -> None:
    scenario = RealScenarioSpec.model_validate(
        {
            "key": "red-before-learning",
            "fixture": "images/adversarial/synthetic-distant-red-meat-pack.png",
            "sha256": "0" * 64,
            "captured_at": "2026-08-25T18:00:00+02:00",
            "expected_kind": "tentative_meal",
        }
    )

    assert scenario_client_version(scenario) == "judge-demo-v1"
    assert scenario_idempotency_key("judge-demo-v1", scenario) == (
        "judge-demo-v1-red-before-learning-v1"
    )


def test_judge_recovery_can_select_one_frozen_scenario_without_renumbering() -> None:
    dataset = load_dataset(MANIFEST, FIXTURE_ROOT)

    selected = selected_real_scenarios(dataset, "cat-negative-control")

    assert [(number, scenario.key) for number, scenario in selected] == [
        (4, "cat-negative-control")
    ]
    with pytest.raises(ValueError, match="unknown judge scenario"):
        selected_real_scenarios(dataset, "missing")


def test_judge_fixture_paths_cannot_escape_the_frozen_fixture_root() -> None:
    with pytest.raises(ValueError, match="escapes"):
        checked_fixture(FIXTURE_ROOT, Path("../../README.md"), "0" * 64)


@pytest.mark.parametrize("citation_field", ["contextual_evidence", "assumptions"])
def test_learned_follow_up_accepts_one_exact_schema_valid_citation(
    citation_field: str,
) -> None:
    revision_id = "knowledge-revision-001"
    activity: dict[str, object] = {
        "activity_hypothesis": {
            "kind": "tentative_meal",
            "confidence": "likely",
            "contextual_evidence": [],
            "assumptions": [],
        }
    }
    hypothesis = activity["activity_hypothesis"]
    assert isinstance(hypothesis, dict)
    if citation_field == "contextual_evidence":
        hypothesis[citation_field] = [
            {
                "source_kind": "household_knowledge",
                "source_id": revision_id,
            }
        ]
    else:
        hypothesis[citation_field] = [{"knowledge_revision_id": revision_id}]

    validate_activity(
        activity,
        scenario=RealScenarioSpec.model_validate(
            {
                "key": "learned-follow-up",
                "fixture": "images/adversarial/synthetic-distant-red-meat-pack.png",
                "sha256": "0" * 64,
                "captured_at": "2026-08-26T18:00:00+02:00",
                "expected_kind": "tentative_meal",
                "must_cite_previous_learning": True,
            }
        ),
        context_note_id="context-note-001",
        previous_knowledge_revision_id=revision_id,
    )


def test_learned_follow_up_rejects_a_missing_exact_citation() -> None:
    with pytest.raises(RuntimeError, match="did not cite the exact correction revision"):
        validate_activity(
            {
                "activity_hypothesis": {
                    "kind": "tentative_meal",
                    "confidence": "likely",
                    "contextual_evidence": [],
                    "assumptions": [],
                }
            },
            scenario=RealScenarioSpec.model_validate(
                {
                    "key": "learned-follow-up",
                    "fixture": "images/adversarial/synthetic-distant-red-meat-pack.png",
                    "sha256": "0" * 64,
                    "captured_at": "2026-08-26T18:00:00+02:00",
                    "expected_kind": "tentative_meal",
                    "must_cite_previous_learning": True,
                }
            ),
            context_note_id="context-note-001",
            previous_knowledge_revision_id="knowledge-revision-001",
        )


def test_synthetic_meal_seed_is_an_exact_idempotent_retry() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("judge-dataset-owner")
        camera = await repository.create_browser_camera(
            "judge-dataset-owner",
            "Judge dataset camera",
            "judge-dataset-camera-instance-v1",
        )
        store = InMemoryObjectStore()
        grouping = CaptureGroupingService(
            repository=repository,
            policy=GroupingPolicy(version="judge-dataset-test-v1"),
        )
        image = (FIXTURE_ROOT / "images" / "synthetic-steak-airfryer.png").read_bytes()
        kwargs = {
            "account": account,
            "camera": camera,
            "image": image,
            "local_at": datetime(
                2026,
                7,
                2,
                18,
                tzinfo=timezone(timedelta(hours=2)),
            ),
            "title": "Steak",
            "sequence_id": "judge-pattern-v1",
            "sequence_number": 1,
            "idempotency_key": "judge-pattern-v1-1",
            "client_version": "judge-pattern-v1",
            "worker_id": "judge-pattern-test",
            "lease_owner": "judge-pattern-publication-test",
            "evidence_description": "Explicitly synthetic no-model history.",
            "rationale": "Explicitly synthetic no-model judge fixture.",
            "capture_id": "00000000-0000-5000-8000-000000000001",
        }

        first = await seed_synthetic_meal(repository, store, grouping, **kwargs)  # type: ignore[arg-type]
        exact_retry = await seed_synthetic_meal(  # type: ignore[arg-type]
            repository,
            store,
            grouping,
            **kwargs,
        )

        assert exact_retry == first
        assert len(await repository.list_meals("judge-dataset-owner")) == 1
        assert len(await repository.recent_captures_for_owner("judge-dataset-owner")) == 1

    asyncio.run(scenario())


def test_seeded_pattern_proposal_uses_only_the_frozen_history() -> None:
    async def scenario() -> None:
        dataset = load_dataset(MANIFEST, FIXTURE_ROOT)
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("judge-pattern-owner")
        camera = await repository.create_browser_camera(
            "judge-pattern-owner",
            "Judge pattern camera",
            "judge-pattern-camera-instance-v1",
        )
        store = InMemoryObjectStore()
        grouping = CaptureGroupingService(
            repository=repository,
            policy=GroupingPolicy(version="judge-pattern-recovery-test-v1"),
        )
        image = (FIXTURE_ROOT / dataset.synthetic_pattern_history.fixture).read_bytes()
        seeded_meal_ids: set[str] = set()
        for ordinal, event in enumerate(dataset.synthetic_pattern_history.events, start=1):
            meal = await seed_synthetic_meal(
                repository,
                store,
                grouping,
                account=account,
                camera=camera,
                image=image,
                local_at=event.captured_at,
                title=event.title,
                sequence_id="judge-pattern-recovery",
                sequence_number=ordinal,
                idempotency_key=f"judge-pattern-recovery-{ordinal}",
                client_version="judge-pattern-recovery-test-v1",
                worker_id="judge-pattern-recovery-test",
                lease_owner="judge-pattern-recovery-publication-test",
                evidence_description="Explicitly synthetic no-model history.",
                rationale="Explicitly synthetic no-model judge fixture.",
            )
            seeded_meal_ids.add(meal.id)

        question = await propose_seeded_pattern(
            repository,
            account_id=account.id,
            seeded_meal_ids=seeded_meal_ids,
            expected_claim_value="steak",
            expected_condition="thursday",
        )

        assert question.pattern_claim is not None
        assert question.pattern_claim.value == "steak"
        assert question.pattern_claim.conditions == ("thursday",)
        assert len(question.pattern_supporting_examples) == 5
        assert len(question.pattern_counterexamples) == 1

    asyncio.run(scenario())
