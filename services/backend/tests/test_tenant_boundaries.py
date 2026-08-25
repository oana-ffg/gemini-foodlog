import asyncio
from datetime import timedelta
from hashlib import sha256

import pytest

from foodlog_backend.app import create_app
from foodlog_backend.errors import CaptureNotFound, CrossAccountAccess
from foodlog_backend.grouping import GroupingPolicy
from foodlog_backend.models import (
    Confidence,
    MealEntry,
    capture_grouping_job_id,
    utc_now,
)
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.settings import Settings


def build_repository() -> InMemoryRepository:
    return InMemoryRepository(public_account_limit=25, trial_image_limit=200)


async def reserve_capture(repository, *, account, camera, suffix: str):
    capture, _, created = await repository.reserve_capture(
        capture_id=f"tenant-capture-{suffix}",
        account=account,
        camera=camera,
        idempotency_key=f"tenant-idempotency-{suffix}",
        content_type="image/jpeg",
        content_sha256=sha256(suffix.encode()).hexdigest(),
        object_key=f"accounts/{account.id}/captures/tenant-capture-{suffix}.jpg",
    )
    assert created is True
    return capture


def meal_for(*, account_id: str, capture_id: str, suffix: str) -> MealEntry:
    return MealEntry(
        id=f"tenant-meal-{suffix}",
        account_id=account_id,
        capture_id=capture_id,
        title=f"Meal {suffix}",
        confidence=Confidence.UNCERTAIN,
        components=[],
        observations=["tenant boundary fixture"],
        alternatives=[],
        rationale="Only account isolation is under test.",
    )


def test_capture_writes_require_the_same_explicit_account_scope() -> None:
    async def scenario() -> None:
        repository = build_repository()
        account_a = await repository.provision_account("tenant-owner-a")
        account_b = await repository.provision_account("tenant-owner-b")
        camera_a = await repository.create_browser_camera(
            "tenant-owner-a", "Kitchen A", "test-browser-instance-0001"
        )
        camera_b = await repository.create_browser_camera(
            "tenant-owner-b", "Kitchen B", "test-browser-instance-0001"
        )
        capture_a = await reserve_capture(
            repository,
            account=account_a,
            camera=camera_a,
            suffix="a",
        )

        with pytest.raises(CrossAccountAccess):
            await reserve_capture(
                repository,
                account=account_a,
                camera=camera_b,
                suffix="forged",
            )
        with pytest.raises(CrossAccountAccess):
            await repository.reserve_capture(
                capture_id="tenant-capture-wrong-object",
                account=account_a,
                camera=camera_a,
                idempotency_key="tenant-wrong-object-key",
                content_type="image/jpeg",
                content_sha256=sha256(b"wrong-object").hexdigest(),
                object_key=(f"accounts/{account_b.id}/captures/tenant-capture-wrong-object.jpg"),
            )
        with pytest.raises(CaptureNotFound):
            await repository.mark_stored(
                account_id=account_b.id,
                capture_id=capture_a.id,
            )
        with pytest.raises(CaptureNotFound):
            await repository.mark_processed(
                account_id=account_b.id,
                capture_id=capture_a.id,
            )
        with pytest.raises(CrossAccountAccess):
            await repository.cancel_capture(
                account_id=account_b.id,
                capture=capture_a,
            )

        assert (await repository.capture_for_owner("tenant-owner-a", capture_a.id)).id == (
            capture_a.id
        )
        assert (await repository.account_for_owner("tenant-owner-a")).accepted_image_count == 1
        assert (await repository.account_for_owner("tenant-owner-b")).accepted_image_count == 0

    asyncio.run(scenario())


def test_derived_records_cannot_attach_to_another_accounts_capture_or_meal() -> None:
    async def scenario() -> None:
        repository = build_repository()
        account_a = await repository.provision_account("derived-owner-a")
        account_b = await repository.provision_account("derived-owner-b")
        camera_a = await repository.create_browser_camera(
            "derived-owner-a", "Kitchen A", "test-browser-instance-0001"
        )
        capture_a = await reserve_capture(
            repository,
            account=account_a,
            camera=camera_a,
            suffix="derived-a",
        )
        meal_a = await repository.save_meal(
            account_id=account_a.id,
            meal=meal_for(
                account_id=account_a.id,
                capture_id=capture_a.id,
                suffix="a",
            ),
        )

        with pytest.raises(CaptureNotFound):
            await repository.save_meal(
                account_id=account_b.id,
                meal=meal_for(
                    account_id=account_b.id,
                    capture_id=capture_a.id,
                    suffix="forged",
                ),
            )
        with pytest.raises(CrossAccountAccess):
            await repository.open_question(
                account_id=account_b.id,
                meal=meal_a,
                prompt="Cross-tenant question",
                reason="This must never be persisted.",
            )

        assert [meal.id for meal in await repository.list_meals("derived-owner-a")] == [meal_a.id]
        assert await repository.list_meals("derived-owner-b") == []
        assert await repository.list_questions("derived-owner-b") == []

    asyncio.run(scenario())


def test_worker_jobs_and_grouping_are_keyed_by_account_and_fail_closed() -> None:
    async def scenario() -> None:
        repository = build_repository()
        account_a = await repository.provision_account("worker-owner-a")
        account_b = await repository.provision_account("worker-owner-b")
        camera_a = await repository.create_browser_camera(
            "worker-owner-a", "Kitchen A", "test-browser-instance-0001"
        )
        capture_a = await reserve_capture(
            repository,
            account=account_a,
            camera=camera_a,
            suffix="worker-a",
        )
        await repository.mark_stored(account_id=account_a.id, capture_id=capture_a.id)
        job_id = capture_grouping_job_id(capture_a.id)

        assert await repository.job_for_account(account_b.id, job_id) is None
        claim = await repository.claim_job(
            account_id=account_b.id,
            job_id=job_id,
            expected_subject_revision=1,
            lease_id="cross-tenant-lease",
            lease_owner="wrong-worker",
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
        grouped = await repository.group_capture(
            account_id=account_b.id,
            capture_id=capture_a.id,
            lease_id="cross-tenant-lease",
            lease_owner="wrong-worker",
            policy=GroupingPolicy(),
        )

        assert claim is None
        assert grouped is None
        assert (await repository.job_for_account(account_a.id, job_id)) is not None
        assert repository._captures[capture_a.id].event_id is None

    asyncio.run(scenario())


def test_public_api_does_not_expose_operational_collections() -> None:
    paths = {route.path for route in create_app(Settings(environment="test")).routes}
    forbidden_fragments = {
        "device_credentials",
        "event_heads",
        "identities",
        "jobs",
        "outbox",
    }

    assert all(fragment not in path for path in paths for fragment in forbidden_fragments)
