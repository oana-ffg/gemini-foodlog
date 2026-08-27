from __future__ import annotations

import logging
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


def inference_settings() -> InferenceWorkerSettings:
    return InferenceWorkerSettings(environment="test", gcp_project_id="test-project")


def test_inference_worker_retries_until_grouped_and_quiet_then_publishes_once() -> None:
    repository, publisher, account_id, capture_id = stored_capture()
    envelope = push_envelope(publisher.events[0].model_dump(mode="json"))
    processor = RecordingInferenceProcessor(repository)
    inference_app = create_inference_worker_app(
        inference_settings(),
        repository=repository,
        processor=processor,
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


def test_inference_worker_rejects_bad_events_and_retries_processor_failures(
    caplog,
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

    with (
        caplog.at_level(
            logging.ERROR,
            logger="foodlog_backend.inference_worker_app",
        ),
        TestClient(app) as client,
    ):
        malformed = client.post(
            "/internal/pubsub/capture-stored",
            json=push_envelope({"kind": "wrong"}),
        )
        failed = client.post("/internal/pubsub/capture-stored", json=envelope)

    assert malformed.status_code == 400
    assert malformed.json() == {"detail": "invalid_pubsub_event"}
    assert failed.status_code == 503
    assert failed.json() == {"detail": "event_inference_failed"}
    assert "simulated model outage" in caplog.text
