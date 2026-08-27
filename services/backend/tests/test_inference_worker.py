from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient

from foodlog_agent.event_processing import ClaimedEventInference
from foodlog_backend.image_worker_app import create_image_worker_app
from foodlog_backend.inference_worker_app import (
    InferenceWorkerSettings,
    create_inference_worker_app,
)
from foodlog_backend.models import JobStatus, event_inference_job_id, utc_now
from tests.test_image_worker import push_envelope, stored_capture, worker_settings


class RecordingInferenceProcessor:
    def __init__(self, repository, *, failure: Exception | None = None) -> None:
        self.repository = repository
        self.failure = failure
        self.calls: list[dict[str, object]] = []
        self.published = 0

    async def process(self, **kwargs) -> ClaimedEventInference | None:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return cast(ClaimedEventInference, object())

    async def publish(self, claimed: ClaimedEventInference):
        del claimed
        self.published += 1
        call = self.calls[-1]
        key = (
            call["account_id"],
            event_inference_job_id(cast(str, call["event_id"])),
        )
        job = self.repository._jobs[key]
        self.repository._jobs[key] = job.model_copy(
            update={"status": JobStatus.COMPLETED, "completed_at": utc_now()},
            deep=True,
        )
        return None


class RecordingPatternDetector:
    def __init__(self, *, failures_remaining: int = 0) -> None:
        self.failures_remaining = failures_remaining
        self.calls: list[str] = []

    async def detect_and_propose(
        self,
        *,
        account_id: str,
        max_proposals: int = 2,
    ) -> list:
        del max_proposals
        self.calls.append(account_id)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("simulated pattern detector outage")
        return []


def inference_settings() -> InferenceWorkerSettings:
    return InferenceWorkerSettings(environment="test", gcp_project_id="test-project")


def test_inference_worker_retries_until_grouped_and_quiet_then_publishes_once() -> None:
    repository, publisher, account_id, capture_id = stored_capture()
    envelope = push_envelope(publisher.events[0].model_dump(mode="json"))
    processor = RecordingInferenceProcessor(repository)
    pattern_detector = RecordingPatternDetector()
    inference_app = create_inference_worker_app(
        inference_settings(),
        repository=repository,
        processor=processor,
        pattern_detector=pattern_detector,
    )

    with TestClient(inference_app) as client:
        health = client.get("/health")
        before_grouping = client.post("/internal/pubsub/capture-stored", json=envelope)

    with TestClient(
        create_image_worker_app(worker_settings(), repository=repository)
    ) as image_client:
        assert (
            image_client.post("/internal/pubsub/capture-stored", json=envelope).status_code == 204
        )

    capture = repository._captures[capture_id]
    assert capture.event_id is not None
    job_key = (account_id, event_inference_job_id(capture.event_id))
    with TestClient(inference_app) as client:
        during_quiet_period = client.post("/internal/pubsub/capture-stored", json=envelope)
        job = repository._jobs[job_key]
        repository._jobs[job_key] = job.model_copy(update={"available_at": utc_now()})
        inferred = client.post("/internal/pubsub/capture-stored", json=envelope)
        duplicate = client.post("/internal/pubsub/capture-stored", json=envelope)

    assert health.json() == {"status": "ok", "mode": "test"}
    assert before_grouping.status_code == 503
    assert before_grouping.json() == {"detail": "capture_not_grouped"}
    assert during_quiet_period.status_code == 503
    assert during_quiet_period.json() == {"detail": "inference_quiet_period"}
    assert inferred.status_code == 204
    assert duplicate.status_code == 204
    assert len(processor.calls) == 1
    assert processor.calls[0]["account_id"] == account_id
    assert processor.calls[0]["event_id"] == capture.event_id
    assert processor.calls[0]["expected_revision"] == 1
    assert processor.published == 1
    assert pattern_detector.calls == [account_id, account_id]


def test_completed_inference_retries_pattern_detection_without_reinvoking_model() -> None:
    repository, publisher, account_id, capture_id = stored_capture()
    envelope = push_envelope(publisher.events[0].model_dump(mode="json"))
    with TestClient(
        create_image_worker_app(worker_settings(), repository=repository)
    ) as image_client:
        assert (
            image_client.post("/internal/pubsub/capture-stored", json=envelope).status_code
            == 204
        )
    capture = repository._captures[capture_id]
    assert capture.event_id is not None
    job_key = (account_id, event_inference_job_id(capture.event_id))
    repository._jobs[job_key] = repository._jobs[job_key].model_copy(
        update={"available_at": utc_now()}
    )
    processor = RecordingInferenceProcessor(repository)
    pattern_detector = RecordingPatternDetector(failures_remaining=1)
    app = create_inference_worker_app(
        inference_settings(),
        repository=repository,
        processor=processor,
        pattern_detector=pattern_detector,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        detector_failed = client.post("/internal/pubsub/capture-stored", json=envelope)
        retry = client.post("/internal/pubsub/capture-stored", json=envelope)

    assert detector_failed.status_code == 503
    assert detector_failed.json() == {"detail": "event_inference_failed"}
    assert retry.status_code == 204
    assert len(processor.calls) == 1
    assert processor.published == 1
    assert pattern_detector.calls == [account_id, account_id]


def test_inference_worker_rejects_bad_events_and_retries_processor_failures(
    capfd,
) -> None:
    repository, publisher, account_id, capture_id = stored_capture()
    envelope = push_envelope(publisher.events[0].model_dump(mode="json"))
    with TestClient(
        create_image_worker_app(worker_settings(), repository=repository)
    ) as image_client:
        assert (
            image_client.post("/internal/pubsub/capture-stored", json=envelope).status_code == 204
        )
    capture = repository._captures[capture_id]
    assert capture.event_id is not None
    job_key = (account_id, event_inference_job_id(capture.event_id))
    repository._jobs[job_key] = repository._jobs[job_key].model_copy(
        update={"available_at": utc_now()}
    )
    processor = RecordingInferenceProcessor(
        repository,
        failure=RuntimeError("simulated model outage"),
    )
    app = create_inference_worker_app(
        inference_settings(),
        repository=repository,
        processor=processor,
    )

    with TestClient(app) as client:
        malformed = client.post(
            "/internal/pubsub/capture-stored",
            json=push_envelope({"kind": "wrong"}),
        )
        failed = client.post("/internal/pubsub/capture-stored", json=envelope)

    assert malformed.status_code == 400
    assert malformed.json() == {"detail": "invalid_pubsub_event"}
    assert failed.status_code == 503
    assert failed.json() == {"detail": "event_inference_failed"}
    output = capfd.readouterr().out
    assert '"event":"event_inference_failed"' in output
    assert '"error_kind":"RuntimeError"' in output
    assert account_id in output
    assert capture.event_id in output
    assert "simulated model outage" not in output
