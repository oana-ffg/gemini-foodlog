from datetime import timedelta

from foodlog_backend.models import (
    CaptureRecord,
    CaptureStatus,
    DurableJob,
    JobKind,
    JobStatus,
    utc_now,
)
from foodlog_backend.processing_views import capture_processing_view


def capture(*, status: CaptureStatus, event_id: str | None = None) -> CaptureRecord:
    return CaptureRecord(
        id="capture-001",
        account_id="account-001",
        camera_id="camera-001",
        idempotency_key="idempotency-001",
        content_type="image/jpeg",
        content_sha256="a" * 64,
        object_key="captures/capture-001.jpg",
        status=status,
        event_id=event_id,
    )


def job(
    *,
    kind: JobKind,
    subject_id: str,
    status: JobStatus = JobStatus.PENDING,
    attempt_count: int = 0,
) -> DurableJob:
    now = utc_now()
    return DurableJob(
        id=f"{kind.value}-{subject_id}",
        account_id="account-001",
        kind=kind,
        subject_id=subject_id,
        subject_revision=1,
        status=status,
        attempt_count=attempt_count,
        available_at=now + timedelta(minutes=5) if attempt_count else now,
        lease_id="lease-001" if status == JobStatus.LEASED else None,
        lease_owner="worker-001" if status == JobStatus.LEASED else None,
        lease_expires_at=now + timedelta(minutes=1) if status == JobStatus.LEASED else None,
        last_error_code="provider_timeout" if attempt_count else None,
        last_error_message="private provider detail" if attempt_count else None,
        completed_at=now if status == JobStatus.COMPLETED else None,
    )


def test_processing_view_preserves_pending_retry_and_attention_states() -> None:
    accepted = capture_processing_view(
        capture(status=CaptureStatus.ACCEPTED),
        grouping_job=None,
        inference_job=None,
    )
    grouping_retry = capture_processing_view(
        capture(status=CaptureStatus.STORED),
        grouping_job=job(
            kind=JobKind.CAPTURE_GROUPING,
            subject_id="capture-001",
            attempt_count=2,
        ),
        inference_job=None,
    )
    missing_inference = capture_processing_view(
        capture(status=CaptureStatus.PROCESSED, event_id="event-001"),
        grouping_job=None,
        inference_job=None,
    )

    assert accepted.stage == "storage_pending"
    assert grouping_retry.stage == "grouping_retrying"
    assert grouping_retry.attempt_count == 2
    assert grouping_retry.latest_failure_code == "provider_timeout"
    assert grouping_retry.retry_at is not None
    assert missing_inference.stage == "attention_required"


def test_processing_view_only_calls_completed_inference_complete() -> None:
    processed = capture(status=CaptureStatus.PROCESSED, event_id="event-001")
    pending = capture_processing_view(
        processed,
        grouping_job=None,
        inference_job=job(kind=JobKind.EVENT_INFERENCE, subject_id="event-001"),
    )
    complete = capture_processing_view(
        processed,
        grouping_job=None,
        inference_job=job(
            kind=JobKind.EVENT_INFERENCE,
            subject_id="event-001",
            status=JobStatus.COMPLETED,
        ),
    )

    assert pending.stage == "analysis_pending"
    assert complete.stage == "complete"
    assert complete.retry_at is None


def test_grouped_stored_capture_reports_the_inference_job_not_grouping_complete() -> None:
    grouped = capture(status=CaptureStatus.STORED, event_id="event-001")
    grouping_complete = job(
        kind=JobKind.CAPTURE_GROUPING,
        subject_id="capture-001",
        status=JobStatus.COMPLETED,
        attempt_count=6,
    )
    inference_retry = job(
        kind=JobKind.EVENT_INFERENCE,
        subject_id="event-001",
        attempt_count=2,
    )

    view = capture_processing_view(
        grouped,
        grouping_job=grouping_complete,
        inference_job=inference_retry,
    )

    assert view.stage == "analysis_retrying"
    assert view.attempt_count == 2
    assert view.latest_failure_code == "provider_timeout"


def test_successful_evaluation_is_not_presented_as_a_failed_product_retry() -> None:
    evaluated = capture(status=CaptureStatus.STORED, event_id="event-001")
    evaluation_job = job(
        kind=JobKind.EVENT_INFERENCE,
        subject_id="event-001",
        attempt_count=2,
    ).model_copy(
        update={
            "last_error_code": "EvaluationComplete",
            "last_error_message": (
                "Evaluation inference completed without publishing a product result."
            ),
        }
    )

    view = capture_processing_view(
        evaluated,
        grouping_job=None,
        inference_job=evaluation_job,
    )

    assert view.stage == "evaluation_complete"
    assert view.attempt_count == 0
    assert view.retry_at is None
    assert view.latest_failure_code is None
