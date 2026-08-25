import asyncio
from datetime import timedelta
from hashlib import sha256

import pytest

import foodlog_backend.repository as repository_module
from foodlog_backend.grouping import CaptureGroupingService, GroupingPolicy
from foodlog_backend.models import (
    ActivityEventStatus,
    CaptureEnvelopeV1,
    DurableJob,
    JobStatus,
    capture_grouping_job_id,
    event_inference_job_id,
    utc_now,
)
from foodlog_backend.repository import InMemoryRepository


class MutableClock:
    def __init__(self) -> None:
        self.current = utc_now()

    def __call__(self):
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


async def add_capture(
    repository: InMemoryRepository,
    *,
    account,
    camera,
    capture_id: str,
    captured_at,
    burst_id: str | None,
    burst_index: int | None,
) -> None:
    metadata = CaptureEnvelopeV1(
        camera_id=camera.id,
        captured_at=captured_at,
        client_kind="browser",
        client_version="grouping-test/1",
        sequence_id="grouping-sequence-0001",
        sequence_number=int(capture_id.rsplit("-", 1)[-1]),
        burst_id=burst_id,
        burst_frame_index=burst_index,
        width=1280,
        height=720,
    )
    capture, _, created = await repository.reserve_capture(
        capture_id=capture_id,
        account=account,
        camera=camera,
        idempotency_key=f"idempotency-{capture_id}",
        content_type="image/jpeg",
        content_sha256=sha256(capture_id.encode()).hexdigest(),
        object_key=f"accounts/{account.id}/captures/{capture_id}.jpg",
        metadata=metadata,
    )
    assert created is True
    await repository.mark_stored(account_id=account.id, capture_id=capture.id)


async def claim_and_group(
    repository: InMemoryRepository,
    *,
    account_id: str,
    capture_id: str,
    clock: MutableClock,
    policy: GroupingPolicy,
):
    lease_id = f"lease-{capture_id}"
    claimed = await repository.claim_job(
        account_id=account_id,
        job_id=capture_grouping_job_id(capture_id),
        expected_subject_revision=1,
        lease_id=lease_id,
        lease_owner="grouping-worker",
        lease_expires_at=clock.current + timedelta(minutes=5),
    )
    assert claimed is not None
    result = await repository.group_capture(
        account_id=account_id,
        capture_id=capture_id,
        lease_id=lease_id,
        lease_owner="grouping-worker",
        policy=policy,
    )
    assert result is not None
    return result


def build_repository() -> InMemoryRepository:
    return InMemoryRepository(public_account_limit=25, trial_image_limit=200)


def test_related_cameras_share_an_account_event_without_crossing_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    policy = GroupingPolicy(quiet_after=timedelta(seconds=30), reopen_window=timedelta(hours=2))

    async def scenario():
        account_a = await repository.provision_account("multi-camera-owner-a")
        account_b = await repository.provision_account("multi-camera-owner-b")
        camera_a1 = await repository.create_browser_camera(
            "multi-camera-owner-a",
            "Sink view",
            "multi-camera-browser-a1",
        )
        camera_a2 = await repository.create_browser_camera(
            "multi-camera-owner-a",
            "Stove view",
            "multi-camera-browser-a2",
        )
        camera_b = await repository.create_browser_camera(
            "multi-camera-owner-b",
            "Other household",
            "multi-camera-browser-b1",
        )

        results = []
        for account, camera, capture_id in (
            (account_a, camera_a1, "multi-camera-capture-1"),
            (account_a, camera_a2, "multi-camera-capture-2"),
            (account_b, camera_b, "multi-camera-capture-3"),
        ):
            await add_capture(
                repository,
                account=account,
                camera=camera,
                capture_id=capture_id,
                captured_at=clock.current,
                burst_id=None,
                burst_index=None,
            )
            clock.advance(timedelta(seconds=1))
            results.append(
                await claim_and_group(
                    repository,
                    account_id=account.id,
                    capture_id=capture_id,
                    clock=clock,
                    policy=policy,
                )
            )
        return camera_a1, camera_a2, results

    camera_a1, camera_a2, results = asyncio.run(scenario())
    first, related, foreign = results

    assert related.event.id == first.event.id
    assert related.event.capture_count == 2
    assert related.event.camera_ids == [camera_a1.id, camera_a2.id]
    assert related.segment.id != first.segment.id
    assert foreign.event.id != first.event.id
    assert foreign.event.account_id != first.event.account_id


def test_one_burst_forms_one_segment_and_one_rescheduled_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    policy = GroupingPolicy(quiet_after=timedelta(seconds=30), reopen_window=timedelta(hours=2))

    async def scenario():
        account = await repository.provision_account("grouping-owner")
        camera = await repository.create_browser_camera(
            "grouping-owner", "Grouping camera", "test-browser-instance-0001"
        )
        started_at = clock.current
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="grouping-capture-1",
            captured_at=started_at,
            burst_id="motion-burst-0001",
            burst_index=0,
        )
        clock.advance(timedelta(seconds=1))
        first = await claim_and_group(
            repository,
            account_id=account.id,
            capture_id="grouping-capture-1",
            clock=clock,
            policy=policy,
        )
        clock.advance(timedelta(seconds=4))
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="grouping-capture-2",
            captured_at=clock.current,
            burst_id="motion-burst-0001",
            burst_index=1,
        )
        clock.advance(timedelta(seconds=1))
        second = await claim_and_group(
            repository,
            account_id=account.id,
            capture_id="grouping-capture-2",
            clock=clock,
            policy=policy,
        )
        return account, started_at, first, second

    account, started_at, first, second = asyncio.run(scenario())

    assert first.event_created is True
    assert first.segment_created is True
    assert second.event_created is False
    assert second.segment_created is False
    assert second.event.id == first.event.id
    assert second.segment.id == first.segment.id
    assert second.event.capture_count == 2
    assert second.segment.capture_count == 2
    assert second.event.current_revision == 2
    assert second.inference_job.subject_revision == 2
    assert second.inference_job.status == JobStatus.PENDING
    assert second.inference_job.available_at == second.event.last_capture_at + timedelta(seconds=30)
    assert second.event.first_capture_at == started_at
    assert repository._captures["grouping-capture-1"].event_id == first.event.id
    assert repository._captures["grouping-capture-2"].segment_id == first.segment.id
    assert (
        repository._jobs[(account.id, capture_grouping_job_id("grouping-capture-1"))].status
        == JobStatus.COMPLETED
    )


def test_new_segment_reopens_the_same_meal_episode_within_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    policy = GroupingPolicy(quiet_after=timedelta(seconds=30), reopen_window=timedelta(hours=2))

    async def scenario():
        account = await repository.provision_account("reopen-owner")
        camera = await repository.create_browser_camera(
            "reopen-owner", "Reopen camera", "test-browser-instance-0001"
        )
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="reopen-capture-1",
            captured_at=clock.current,
            burst_id="reopen-burst-0001",
            burst_index=0,
        )
        clock.advance(timedelta(seconds=1))
        first = await claim_and_group(
            repository,
            account_id=account.id,
            capture_id="reopen-capture-1",
            clock=clock,
            policy=policy,
        )
        repository._events[(account.id, first.event.id)] = first.event.model_copy(
            update={
                "status": ActivityEventStatus.INFERRED,
                "meal_id": "existing-meal-0001",
            }
        )
        clock.advance(timedelta(minutes=45))
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="reopen-capture-2",
            captured_at=clock.current,
            burst_id="reopen-burst-0002",
            burst_index=0,
        )
        clock.advance(timedelta(seconds=1))
        reopened = await claim_and_group(
            repository,
            account_id=account.id,
            capture_id="reopen-capture-2",
            clock=clock,
            policy=policy,
        )
        return account, first, reopened

    account, first, reopened = asyncio.run(scenario())

    assert reopened.event.id == first.event.id
    assert reopened.segment.id != first.segment.id
    assert reopened.segment_created is True
    assert reopened.event.status == ActivityEventStatus.OPEN
    assert reopened.event.meal_id == "existing-meal-0001"
    assert reopened.event.current_revision == 2
    inference_job = repository._jobs[(account.id, event_inference_job_id(first.event.id))]
    assert inference_job.subject_revision == 2
    assert inference_job.status == JobStatus.PENDING


def test_activity_after_reopen_window_starts_a_new_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    policy = GroupingPolicy(quiet_after=timedelta(seconds=30), reopen_window=timedelta(hours=2))

    async def scenario():
        account = await repository.provision_account("separate-owner")
        camera = await repository.create_browser_camera(
            "separate-owner", "Separate camera", "test-browser-instance-0001"
        )
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="separate-capture-1",
            captured_at=clock.current,
            burst_id="separate-burst-0001",
            burst_index=0,
        )
        clock.advance(timedelta(seconds=1))
        first = await claim_and_group(
            repository,
            account_id=account.id,
            capture_id="separate-capture-1",
            clock=clock,
            policy=policy,
        )
        clock.advance(timedelta(hours=2, seconds=1))
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="separate-capture-2",
            captured_at=clock.current,
            burst_id="separate-burst-0002",
            burst_index=0,
        )
        clock.advance(timedelta(seconds=1))
        second = await claim_and_group(
            repository,
            account_id=account.id,
            capture_id="separate-capture-2",
            clock=clock,
            policy=policy,
        )
        return first, second

    first, second = asyncio.run(scenario())

    assert first.event.id != second.event.id
    assert second.event_created is True
    assert second.event.current_revision == 1


def test_event_inference_job_becomes_claimable_only_after_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    policy = GroupingPolicy(quiet_after=timedelta(seconds=30), reopen_window=timedelta(hours=2))

    async def scenario():
        account = await repository.provision_account("quiet-owner")
        camera = await repository.create_browser_camera(
            "quiet-owner", "Quiet camera", "test-browser-instance-0001"
        )
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="quiet-capture-1",
            captured_at=clock.current,
            burst_id="quiet-burst-0001",
            burst_index=0,
        )
        clock.advance(timedelta(seconds=1))
        grouped = await claim_and_group(
            repository,
            account_id=account.id,
            capture_id="quiet-capture-1",
            clock=clock,
            policy=policy,
        )
        job_id = event_inference_job_id(grouped.event.id)
        early = await repository.claim_job(
            account_id=account.id,
            job_id=job_id,
            expected_subject_revision=1,
            lease_id="early-inference-lease",
            lease_owner="inference-worker",
            lease_expires_at=clock.current + timedelta(minutes=5),
        )
        clock.current = grouped.inference_job.available_at
        due = await repository.claim_job(
            account_id=account.id,
            job_id=job_id,
            expected_subject_revision=1,
            lease_id="due-inference-lease",
            lease_owner="inference-worker",
            lease_expires_at=clock.current + timedelta(minutes=5),
        )
        return early, due

    early, due = asyncio.run(scenario())

    assert early is None
    assert isinstance(due, DurableJob)
    assert due.status == JobStatus.LEASED


def test_grouping_rejects_wrong_or_expired_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    policy = GroupingPolicy()

    async def scenario():
        account = await repository.provision_account("lease-owner")
        camera = await repository.create_browser_camera(
            "lease-owner", "Lease camera", "test-browser-instance-0001"
        )
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="lease-capture-1",
            captured_at=clock.current,
            burst_id=None,
            burst_index=None,
        )
        clock.advance(timedelta(seconds=1))
        claimed = await repository.claim_job(
            account_id=account.id,
            job_id=capture_grouping_job_id("lease-capture-1"),
            expected_subject_revision=1,
            lease_id="valid-grouping-lease",
            lease_owner="grouping-worker",
            lease_expires_at=clock.current + timedelta(seconds=5),
        )
        assert claimed is not None
        wrong = await repository.group_capture(
            account_id=account.id,
            capture_id="lease-capture-1",
            lease_id="wrong-lease",
            lease_owner="grouping-worker",
            policy=policy,
        )
        clock.advance(timedelta(seconds=6))
        expired = await repository.group_capture(
            account_id=account.id,
            capture_id="lease-capture-1",
            lease_id="valid-grouping-lease",
            lease_owner="grouping-worker",
            policy=policy,
        )
        return wrong, expired

    wrong, expired = asyncio.run(scenario())
    assert wrong is None
    assert expired is None


def test_grouping_service_claims_once_and_duplicate_delivery_is_a_noop() -> None:
    repository = build_repository()

    async def scenario():
        account = await repository.provision_account("service-owner")
        camera = await repository.create_browser_camera(
            "service-owner", "Service camera", "test-browser-instance-0001"
        )
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="service-capture-1",
            captured_at=utc_now(),
            burst_id="service-burst-0001",
            burst_index=0,
        )
        service = CaptureGroupingService(
            repository=repository,
            policy=GroupingPolicy(),
        )
        first = await service.process(
            account_id=account.id,
            capture_id="service-capture-1",
            worker_id="service-worker",
        )
        duplicate = await service.process(
            account_id=account.id,
            capture_id="service-capture-1",
            worker_id="service-worker",
        )
        return first, duplicate

    first, duplicate = asyncio.run(scenario())
    assert first is not None
    assert duplicate is None


def test_grouping_service_releases_failures_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()

    async def scenario():
        account = await repository.provision_account("failure-owner")
        camera = await repository.create_browser_camera(
            "failure-owner", "Failure camera", "test-browser-instance-0001"
        )
        await add_capture(
            repository,
            account=account,
            camera=camera,
            capture_id="failure-capture-1",
            captured_at=utc_now(),
            burst_id=None,
            burst_index=None,
        )

        async def fail_grouping(**_):
            raise RuntimeError("simulated grouping failure")

        monkeypatch.setattr(repository, "group_capture", fail_grouping)
        service = CaptureGroupingService(
            repository=repository,
            policy=GroupingPolicy(),
        )
        with pytest.raises(RuntimeError, match="simulated grouping failure"):
            await service.process(
                account_id=account.id,
                capture_id="failure-capture-1",
                worker_id="service-worker",
            )
        return account

    account = asyncio.run(scenario())
    job = repository._jobs[(account.id, capture_grouping_job_id("failure-capture-1"))]
    assert job.status == JobStatus.PENDING
    assert job.attempt_count == 1
    assert job.last_error_code == "RuntimeError"
    assert job.last_error_message == "simulated grouping failure"
