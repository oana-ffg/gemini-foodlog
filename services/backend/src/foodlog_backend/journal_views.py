from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .models import ActivityEvent, CaptureRecord, DurableJob, JobStatus

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
) -> JournalEventView:
    if not captures or len(captures) != event.capture_count:
        raise ValueError("Journal event evidence is incomplete")
    if any(capture.account_id != event.account_id for capture in captures):
        raise ValueError("Journal event evidence escaped its account scope")
    if any(capture.event_id != event.id for capture in captures):
        raise ValueError("Journal event evidence escaped its event scope")

    failed = inference_job is None or inference_job.status in {
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
            inference_job.last_error_code if inference_job is not None and failed else None
        ),
    )
