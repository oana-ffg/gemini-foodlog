import asyncio
from datetime import timedelta
from hashlib import sha256

import pytest

import foodlog_backend.repository as repository_module
from foodlog_backend.errors import JobIdentityConflict
from foodlog_backend.models import (
    DurableJob,
    JobKind,
    JobStatus,
    capture_grouping_job_id,
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


async def stored_capture_job(repository: InMemoryRepository) -> DurableJob:
    account = await repository.provision_account("job-owner")
    camera = await repository.create_browser_camera(
        "job-owner", "Job test camera", "test-browser-instance-0001"
    )
    capture, _, created = await repository.reserve_capture(
        capture_id="capture-for-job-0001",
        account=account,
        camera=camera,
        idempotency_key="capture-job-idempotency-0001",
        content_type="image/jpeg",
        content_sha256=sha256(b"job-image").hexdigest(),
        object_key=f"accounts/{account.id}/captures/capture-for-job-0001.jpg",
    )
    assert created is True
    await repository.mark_stored(account_id=account.id, capture_id=capture.id)
    job = await repository.job_for_account(
        account.id,
        capture_grouping_job_id(capture.id),
    )
    assert job is not None
    return job


def build_repository() -> InMemoryRepository:
    return InMemoryRepository(public_account_limit=25, trial_image_limit=200)


def test_storing_a_capture_transactionally_enqueues_one_grouping_job() -> None:
    repository = build_repository()
    job = asyncio.run(stored_capture_job(repository))

    asyncio.run(repository.mark_stored(account_id=job.account_id, capture_id=job.subject_id))
    stored = asyncio.run(repository.job_for_account(job.account_id, job.id))

    assert stored is not None
    assert stored.kind == JobKind.CAPTURE_GROUPING
    assert stored.subject_id == "capture-for-job-0001"
    assert stored.subject_revision == 1
    assert stored.status == JobStatus.PENDING
    assert stored.attempt_count == 0
    assert len(repository._jobs) == 1


def test_competing_workers_get_exactly_one_active_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    job = asyncio.run(stored_capture_job(repository))
    clock.advance(timedelta(seconds=1))

    async def compete():
        return await asyncio.gather(
            repository.claim_job(
                account_id=job.account_id,
                job_id=job.id,
                expected_subject_revision=1,
                lease_id="lease-a",
                lease_owner="worker-a",
                lease_expires_at=clock.current + timedelta(minutes=5),
            ),
            repository.claim_job(
                account_id=job.account_id,
                job_id=job.id,
                expected_subject_revision=1,
                lease_id="lease-b",
                lease_owner="worker-b",
                lease_expires_at=clock.current + timedelta(minutes=5),
            ),
        )

    claims = asyncio.run(compete())
    winner = next(claim for claim in claims if claim is not None)

    assert sum(claim is not None for claim in claims) == 1
    assert winner.status == JobStatus.LEASED
    assert winner.attempt_count == 1
    assert asyncio.run(
        repository.complete_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id=winner.lease_id or "",
            lease_owner=winner.lease_owner or "",
        )
    )
    assert not asyncio.run(
        repository.complete_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id=winner.lease_id or "",
            lease_owner=winner.lease_owner or "",
        )
    )


def test_expired_worker_cannot_publish_after_another_worker_reclaims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    job = asyncio.run(stored_capture_job(repository))
    clock.advance(timedelta(seconds=1))
    first = asyncio.run(
        repository.claim_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id="expired-lease",
            lease_owner="old-worker",
            lease_expires_at=clock.current + timedelta(minutes=5),
        )
    )
    assert first is not None

    clock.advance(timedelta(minutes=6))
    second = asyncio.run(
        repository.claim_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id="replacement-lease",
            lease_owner="new-worker",
            lease_expires_at=clock.current + timedelta(minutes=5),
        )
    )

    assert second is not None
    assert second.attempt_count == 2
    assert not asyncio.run(
        repository.complete_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id="expired-lease",
            lease_owner="old-worker",
        )
    )
    assert asyncio.run(
        repository.complete_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id="replacement-lease",
            lease_owner="new-worker",
        )
    )


def test_retry_waits_until_available_and_preserves_attempt_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    job = asyncio.run(stored_capture_job(repository))
    clock.advance(timedelta(seconds=1))
    claim = asyncio.run(
        repository.claim_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id="retry-lease-1",
            lease_owner="worker-a",
            lease_expires_at=clock.current + timedelta(minutes=5),
        )
    )
    assert claim is not None
    retry_at = clock.current + timedelta(minutes=10)
    assert asyncio.run(
        repository.release_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id="retry-lease-1",
            lease_owner="worker-a",
            available_at=retry_at,
            error_code="temporary_error",
            error_message="Temporary downstream failure",
        )
    )

    assert (
        asyncio.run(
            repository.claim_job(
                account_id=job.account_id,
                job_id=job.id,
                expected_subject_revision=1,
                lease_id="retry-too-early",
                lease_owner="worker-b",
                lease_expires_at=clock.current + timedelta(minutes=5),
            )
        )
        is None
    )
    clock.advance(timedelta(minutes=10))
    retry = asyncio.run(
        repository.claim_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id="retry-lease-2",
            lease_owner="worker-b",
            lease_expires_at=clock.current + timedelta(minutes=5),
        )
    )

    assert retry is not None
    assert retry.attempt_count == 2
    assert retry.last_error_code is None
    assert retry.last_error_message is None


def test_new_subject_revision_invalidates_stale_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_repository()
    clock = MutableClock()
    monkeypatch.setattr(repository_module, "utc_now", clock)
    job = asyncio.run(stored_capture_job(repository))
    clock.advance(timedelta(seconds=1))
    first = asyncio.run(
        repository.claim_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id="revision-one-lease",
            lease_owner="worker-a",
            lease_expires_at=clock.current + timedelta(minutes=5),
        )
    )
    assert first is not None
    revision_two = asyncio.run(
        repository.enqueue_job(
            DurableJob(
                id=job.id,
                account_id=job.account_id,
                kind=job.kind,
                subject_id=job.subject_id,
                subject_revision=2,
                available_at=clock.current,
                created_at=clock.current,
            )
        )
    )

    assert revision_two.status == JobStatus.PENDING
    assert revision_two.subject_revision == 2
    assert revision_two.created_at == job.created_at
    assert not asyncio.run(
        repository.complete_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id="revision-one-lease",
            lease_owner="worker-a",
        )
    )
    assert (
        asyncio.run(
            repository.claim_job(
                account_id=job.account_id,
                job_id=job.id,
                expected_subject_revision=1,
                lease_id="stale-revision-lease",
                lease_owner="worker-b",
                lease_expires_at=clock.current + timedelta(minutes=5),
            )
        )
        is None
    )
    current = asyncio.run(
        repository.claim_job(
            account_id=job.account_id,
            job_id=job.id,
            expected_subject_revision=2,
            lease_id="revision-two-lease",
            lease_owner="worker-b",
            lease_expires_at=clock.current + timedelta(minutes=5),
        )
    )
    assert current is not None


def test_job_identity_and_account_scope_cannot_be_reused() -> None:
    repository = build_repository()
    job = asyncio.run(stored_capture_job(repository))

    with pytest.raises(JobIdentityConflict):
        asyncio.run(
            repository.enqueue_job(
                DurableJob(
                    id=job.id,
                    account_id=job.account_id,
                    kind=job.kind,
                    subject_id="different-subject",
                    subject_revision=2,
                )
            )
        )

    assert (
        asyncio.run(
            repository.claim_job(
                account_id="different-account",
                job_id=job.id,
                expected_subject_revision=1,
                lease_id="cross-account-lease",
                lease_owner="worker-a",
                lease_expires_at=utc_now() + timedelta(minutes=5),
            )
        )
        is None
    )


def test_enqueue_cannot_bypass_transactional_claiming() -> None:
    repository = build_repository()
    now = utc_now()
    preleased = DurableJob(
        id="preleased-job",
        account_id="account-a",
        kind=JobKind.CAPTURE_GROUPING,
        subject_id="capture-a",
        subject_revision=1,
        status=JobStatus.LEASED,
        attempt_count=1,
        lease_id="injected-lease",
        lease_owner="untrusted-worker",
        lease_expires_at=now + timedelta(minutes=5),
        created_at=now,
    )

    with pytest.raises(ValueError, match="clean pending work"):
        asyncio.run(repository.enqueue_job(preleased))
