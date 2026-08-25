from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256

from .models import ActivityEvent, ActivitySegment, CaptureRecord, DurableJob


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
