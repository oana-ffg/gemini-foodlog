from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from .models import ActivityEvent, CaptureRecord, DurableJob, JobStatus, utc_now

JournalEventState = Literal["processing", "error_processing"]


class JournalEventView(BaseModel):
    event_id: str
    event_revision: int = Field(ge=1)
    captured_at: datetime
    camera_ids: list[str] = Field(min_length=1, max_length=8)
    capture_ids: list[str] = Field(min_length=1)
    state: JournalEventState
    latest_failure_code: str | None = None


def journal_event_view(
    event: ActivityEvent,
    captures: list[CaptureRecord],
    *,
    inference_job: DurableJob | None,
    now: datetime | None = None,
) -> JournalEventView:
    if not captures or len(captures) != event.capture_count:
        raise ValueError("Journal event evidence is incomplete")
    if any(capture.account_id != event.account_id for capture in captures):
        raise ValueError("Journal event evidence escaped its account scope")
    if any(capture.event_id != event.id for capture in captures):
        raise ValueError("Journal event evidence escaped its event scope")

    current_time = now or utc_now()
    exceeded_processing_window = current_time - event.first_capture_at >= timedelta(days=1)
    failed = exceeded_processing_window or inference_job is None or inference_job.status in {
        JobStatus.FAILED,
        JobStatus.COMPLETED,
    }
    return JournalEventView(
        event_id=event.id,
        event_revision=event.current_revision,
        captured_at=event.first_capture_at,
        camera_ids=event.camera_ids,
        capture_ids=[capture.id for capture in captures],
        state="error_processing" if failed else "processing",
        latest_failure_code=(
            "ProcessingDeadlineExceeded"
            if exceeded_processing_window
            and (inference_job is None or inference_job.status != JobStatus.FAILED)
            else inference_job.last_error_code
            if inference_job is not None and failed
            else None
        ),
    )
