from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from foodlog_backend.grouping import CaptureGroupingService, GroupingPolicy
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.storage import InMemoryObjectStore
from scripts.prepare_judge_dataset import checked_fixture, load_dataset
from scripts.synthetic_dataset_support import seed_synthetic_meal

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures"
MANIFEST = FIXTURE_ROOT / "judge-demo-dataset.v1.json"


def test_judge_dataset_manifest_is_complete_and_hash_locked() -> None:
    dataset = load_dataset(MANIFEST, FIXTURE_ROOT)

    assert dataset.dataset_id == "judge-demo-v1"
    assert [item.key for item in dataset.real_inference_scenarios] == [
        "red-before-learning",
        "red-after-learning",
        "ambiguous-with-context",
        "cat-negative-control",
    ]
    assert len(dataset.synthetic_pattern_history.events) == 6
    assert dataset.synthetic_pattern_history.expected_claim_value == "Steak"
    assert all(
        event.captured_at.weekday() == 3
        for event in dataset.synthetic_pattern_history.events
    )


def test_judge_fixture_paths_cannot_escape_the_frozen_fixture_root() -> None:
    with pytest.raises(ValueError, match="escapes"):
        checked_fixture(FIXTURE_ROOT, Path("../../README.md"), "0" * 64)


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
