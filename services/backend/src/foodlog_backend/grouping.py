from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from .models import (
    ActivityEvent,
    ActivitySegment,
    CaptureRecord,
    DurableJob,
    capture_grouping_job_id,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class GroupingPolicy:
    version: str = "temporal-v1"
    quiet_after: timedelta = timedelta(seconds=30)
    reopen_window: timedelta = timedelta(hours=2)

    def __post_init__(self) -> None:
        if not self.version or len(self.version) > 80:
            raise ValueError("grouping policy version must contain 1 to 80 characters")
        if self.quiet_after <= timedelta(0):
            raise ValueError("quiet_after must be positive")
        if self.reopen_window < self.quiet_after:
            raise ValueError("reopen_window must not be shorter than quiet_after")


@dataclass(frozen=True, slots=True)
class CaptureGroupingResult:
    event: ActivityEvent
    segment: ActivitySegment
    inference_job: DurableJob
    event_created: bool
    segment_created: bool


class CaptureGroupingRepository(Protocol):
    async def claim_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> DurableJob | None: ...

    async def group_capture(
        self,
        *,
        account_id: str,
        capture_id: str,
        lease_id: str,
        lease_owner: str,
        policy: GroupingPolicy,
    ) -> CaptureGroupingResult | None: ...

    async def release_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        available_at: datetime,
        error_code: str,
        error_message: str,
    ) -> bool: ...


class CaptureGroupingService:
    def __init__(
        self,
        *,
        repository: CaptureGroupingRepository,
        policy: GroupingPolicy,
        lease_duration: timedelta = timedelta(minutes=5),
        retry_delay: timedelta = timedelta(seconds=10),
    ) -> None:
        if lease_duration <= timedelta(0) or retry_delay < timedelta(0):
            raise ValueError("grouping lease and retry durations are invalid")
        self._repository = repository
        self._policy = policy
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay

    async def process(
        self,
        *,
        account_id: str,
        capture_id: str,
        worker_id: str,
    ) -> CaptureGroupingResult | None:
        lease_id = str(uuid4())
        job_id = capture_grouping_job_id(capture_id)
        claimed = await self._repository.claim_job(
            account_id=account_id,
            job_id=job_id,
            expected_subject_revision=1,
            lease_id=lease_id,
            lease_owner=worker_id,
            lease_expires_at=utc_now() + self._lease_duration,
        )
        if claimed is None:
            return None
        try:
            result = await self._repository.group_capture(
                account_id=account_id,
                capture_id=capture_id,
                lease_id=lease_id,
                lease_owner=worker_id,
                policy=self._policy,
            )
        except Exception as error:
            await self._repository.release_job(
                account_id=account_id,
                job_id=job_id,
                expected_subject_revision=1,
                lease_id=lease_id,
                lease_owner=worker_id,
                available_at=utc_now() + self._retry_delay,
                error_code=type(error).__name__[:120],
                error_message=str(error)[:2_000] or type(error).__name__,
            )
            raise
        if result is None:
            raise RuntimeError("capture grouping lease was lost")
        return result


def capture_activity_time(capture: CaptureRecord):
    return capture.metadata.captured_at if capture.metadata is not None else capture.created_at


def segment_identity(capture: CaptureRecord) -> tuple[str, str]:
    if capture.metadata is not None and capture.metadata.burst_id is not None:
        source_key = f"burst:{capture.metadata.burst_id}"
    else:
        source_key = f"capture:{capture.id}"
    digest = sha256(
        f"{capture.account_id}\0{capture.camera_id}\0{source_key}".encode()
    ).hexdigest()
    return f"segment-{digest}", source_key
