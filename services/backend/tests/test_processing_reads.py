from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from foodlog_backend.app import create_app
from foodlog_backend.models import (
    CaptureRecord,
    CaptureStatus,
    DurableJob,
    JobKind,
    JobStatus,
    capture_grouping_job_id,
    event_inference_job_id,
    utc_now,
)
from foodlog_backend.settings import Settings


@pytest.mark.parametrize("missing_job", [False, True])
def test_processing_reads_each_burst_job_once_per_request(
    monkeypatch: pytest.MonkeyPatch,
    missing_job: bool,
) -> None:
    app = create_app(Settings(environment="test"))
    repository = app.state.container.repository
    captures = [
        CaptureRecord(
            id=f"burst-frame-{index}",
            account_id="account-a",
            camera_id="camera-a",
            idempotency_key=f"burst-frame-{index}",
            content_type="image/jpeg",
            content_sha256="a" * 64,
            object_key=f"captures/{index}.jpg",
            status=CaptureStatus.PROCESSED,
            event_id="event-a",
        )
        for index in range(20)
    ]
    job = DurableJob(
        id=event_inference_job_id("event-a"),
        account_id="account-a",
        kind=JobKind.EVENT_INFERENCE,
        subject_id="event-a",
        subject_revision=1,
    )
    recent = AsyncMock(return_value=captures)
    lookup = AsyncMock(return_value=None if missing_job else job)
    monkeypatch.setattr(repository, "recent_captures_for_owner", recent)
    monkeypatch.setattr(repository, "job_for_account", lookup)
    with TestClient(app) as client:
        headers = {"X-FoodLog-Local-User": "owner-a"}
        first = client.get("/v1/processing", headers=headers)
        assert first.status_code == 200
        assert len(first.json()) == 20
        assert {view["stage"] for view in first.json()} == {
            "attention_required" if missing_job else "analysis_pending"
        }
        assert lookup.await_count == 1
        lookup.assert_awaited_with("account-a", event_inference_job_id("event-a"))

        # A later request must observe new job state, including previously missing jobs.
        lookup.return_value = job.model_copy(
            update={
                "status": JobStatus.COMPLETED,
                "completed_at": utc_now(),
            }
        )
        second = client.get("/v1/processing", headers=headers)
        assert second.status_code == 200
        assert {view["stage"] for view in second.json()} == {"complete"}
        assert lookup.await_count == 2


def test_processing_only_reads_grouping_jobs_for_ungrouped_stored_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(Settings(environment="test"))
    repository = app.state.container.repository
    captures = [
        CaptureRecord(
            id=f"frame-{status.value}",
            account_id="account-a",
            camera_id="camera-a",
            idempotency_key=f"frame-{status.value}",
            content_type="image/jpeg",
            content_sha256="a" * 64,
            object_key=f"captures/{status.value}.jpg",
            status=status,
        )
        for status in (CaptureStatus.ACCEPTED, CaptureStatus.STORED, CaptureStatus.PROCESSED)
    ]
    monkeypatch.setattr(repository, "recent_captures_for_owner", AsyncMock(return_value=captures))
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(repository, "job_for_account", lookup)
    with TestClient(app) as client:
        response = client.get("/v1/processing", headers={"X-FoodLog-Local-User": "owner-a"})
    assert response.status_code == 200
    assert [view["stage"] for view in response.json()] == [
        "storage_pending",
        "attention_required",
        "attention_required",
    ]
    lookup.assert_awaited_once_with("account-a", capture_grouping_job_id("frame-stored"))
