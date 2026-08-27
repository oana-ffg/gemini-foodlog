from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .models import CaptureRecord, CaptureStatus, DurableJob, JobStatus, utc_now

ProcessingStage = Literal[
    "storage_pending",
    "grouping_pending",
    "grouping_active",
    "grouping_retrying",
    "analysis_pending",
    "analysis_active",
    "analysis_retrying",
    "complete",
    "attention_required",
]


class CaptureProcessingView(BaseModel):
    capture_id: str
    camera_id: str
    captured_at: datetime
    stage: ProcessingStage
    attempt_count: int = Field(ge=0)
    retry_at: datetime | None = None
    latest_failure_code: str | None = None


def capture_processing_view(
    capture: CaptureRecord,
    *,
    grouping_job: DurableJob | None,
    inference_job: DurableJob | None,
    now: datetime | None = None,
) -> CaptureProcessingView:
    current_time = now or utc_now()

    if capture.status == CaptureStatus.ACCEPTED:
        return _view(capture, "storage_pending")

    if capture.status == CaptureStatus.STORED:
        if grouping_job is None or grouping_job.status == JobStatus.COMPLETED:
            return _view(capture, "attention_required", grouping_job)
        return _view(
            capture,
            _job_stage(
                grouping_job,
                pending="grouping_pending",
                active="grouping_active",
                retrying="grouping_retrying",
                now=current_time,
            ),
            grouping_job,
        )

    if capture.event_id is None or inference_job is None:
        return _view(capture, "attention_required", inference_job)
    if inference_job.status == JobStatus.COMPLETED:
        return _view(capture, "complete", inference_job)
    return _view(
        capture,
        _job_stage(
            inference_job,
            pending="analysis_pending",
            active="analysis_active",
            retrying="analysis_retrying",
            now=current_time,
        ),
        inference_job,
    )


def _job_stage(
    job: DurableJob,
    *,
    pending: ProcessingStage,
    active: ProcessingStage,
    retrying: ProcessingStage,
    now: datetime,
) -> ProcessingStage:
    if job.status == JobStatus.LEASED:
        return active
    if job.attempt_count > 0 or job.available_at > now:
        return retrying
    return pending


def _view(
    capture: CaptureRecord,
    stage: ProcessingStage,
    job: DurableJob | None = None,
) -> CaptureProcessingView:
    return CaptureProcessingView(
        capture_id=capture.id,
        camera_id=capture.camera_id,
        captured_at=capture.created_at,
        stage=stage,
        attempt_count=job.attempt_count if job else 0,
        retry_at=(
            job.available_at
            if job and job.status == JobStatus.PENDING and job.attempt_count > 0
            else None
        ),
        latest_failure_code=job.last_error_code if job else None,
    )
