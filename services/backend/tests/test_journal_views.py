from datetime import timedelta

from foodlog_backend.journal_views import journal_event_view
from foodlog_backend.models import (
    ActivityEvent,
    CaptureRecord,
    CaptureStatus,
    DurableJob,
    JobKind,
    JobStatus,
    utc_now,
)


def unresolved_fixture(*, age: timedelta) -> tuple[ActivityEvent, CaptureRecord, DurableJob]:
    captured_at = utc_now() - age
    event = ActivityEvent(
        id="journal-view-event",
        account_id="journal-view-account",
        camera_ids=["journal-view-camera"],
        first_capture_at=captured_at,
        last_capture_at=captured_at,
        capture_count=1,
        grouping_policy_version="journal-view-test-v1",
        created_at=captured_at,
        updated_at=captured_at,
    )
    capture = CaptureRecord(
        id="journal-view-capture",
        account_id=event.account_id,
        camera_id=event.camera_ids[0],
        idempotency_key="journal-view-idempotency",
        content_type="image/jpeg",
        content_sha256="a" * 64,
        object_key="accounts/journal-view-account/captures/journal-view-capture.jpg",
        event_id=event.id,
        status=CaptureStatus.STORED,
        created_at=captured_at,
    )
    job = DurableJob(
        id="event-inference-journal-view-event",
        account_id=event.account_id,
        kind=JobKind.EVENT_INFERENCE,
        subject_id=event.id,
        subject_revision=1,
        status=JobStatus.PENDING,
        created_at=captured_at,
        available_at=captured_at,
    )
    return event, capture, job


def test_recent_retryable_event_stays_processing() -> None:
    now = utc_now()
    event, capture, job = unresolved_fixture(age=timedelta(hours=23, minutes=59))

    view = journal_event_view(event, [capture], inference_job=job, now=now)

    assert view.state == "processing"
    assert view.latest_failure_code is None


def test_retryable_event_becomes_error_by_one_day() -> None:
    now = utc_now()
    event, capture, job = unresolved_fixture(age=timedelta(days=1, seconds=1))

    view = journal_event_view(event, [capture], inference_job=job, now=now)

    assert view.state == "error_processing"
    assert view.latest_failure_code == "ProcessingDeadlineExceeded"
