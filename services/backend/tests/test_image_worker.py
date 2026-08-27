import asyncio
import json
from base64 import b64encode

import pytest
from fastapi.testclient import TestClient

from foodlog_backend.image_events import InMemoryCaptureEventPublisher
from foodlog_backend.image_worker_app import ImageWorkerSettings, create_image_worker_app
from foodlog_backend.models import JobStatus, capture_grouping_job_id
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.service import CaptureService
from foodlog_backend.storage import InMemoryObjectStore


def stored_capture(
    *,
    owner_user_id: str = "image-worker-owner",
) -> tuple[
    InMemoryRepository,
    InMemoryCaptureEventPublisher,
    str,
    str,
]:
    async def prepare():
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        publisher = InMemoryCaptureEventPublisher()
        service = CaptureService(
            repository=repository,
            object_store=InMemoryObjectStore(),
            event_publisher=publisher,
        )
        account = await repository.provision_account(owner_user_id)
        camera = await repository.create_browser_camera(
            owner_user_id, "Kitchen", "test-browser-instance-0001"
        )
        accepted = await service.accept_capture(
            owner_user_id=owner_user_id,
            camera=camera,
            idempotency_key="image-worker-capture-0001",
            content_type="image/png",
            image=b"stored-image-evidence",
            metadata=None,
        )
        return repository, publisher, account.id, accepted.capture_id

    return asyncio.run(prepare())


def push_envelope(payload: dict, *, message_id: str = "image-message-1") -> dict:
    return {
        "message": {
            "data": b64encode(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).decode(),
            "messageId": message_id,
        },
        "subscription": "projects/test/subscriptions/foodlog-image-consumer",
    }


def worker_settings() -> ImageWorkerSettings:
    return ImageWorkerSettings(environment="test", gcp_project_id="test-project")


def test_capture_publish_failure_retains_stored_work_for_idempotent_retry() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        publisher = InMemoryCaptureEventPublisher()
        publisher.failure = RuntimeError("simulated Pub/Sub outage")
        service = CaptureService(
            repository=repository,
            object_store=InMemoryObjectStore(),
            event_publisher=publisher,
        )
        account = await repository.provision_account("retry-owner")
        camera = await repository.create_browser_camera(
            "retry-owner", "Kitchen", "test-browser-instance-0001"
        )
        request = {
            "owner_user_id": "retry-owner",
            "camera": camera,
            "idempotency_key": "image-publish-retry-0001",
            "content_type": "image/png",
            "image": b"retained-image-evidence",
            "metadata": None,
        }

        with pytest.raises(RuntimeError, match="Pub/Sub outage"):
            await service.accept_capture(**request)

        capture = next(iter(repository._captures.values()))
        assert capture.status == "stored"
        assert repository._accounts[account.id].accepted_image_count == 1
        assert repository._jobs[(account.id, capture_grouping_job_id(capture.id))].status == (
            JobStatus.PENDING
        )

        publisher.failure = None
        recovered = await service.accept_capture(**request)

        assert recovered.capture_id == capture.id
        assert recovered.duplicate is True
        assert recovered.accepted_image_count == 1
        assert [event.capture_id for event in publisher.events] == [capture.id]
        assert publisher.events[0].account_id == account.id

    asyncio.run(scenario())


def test_image_worker_groups_once_and_acknowledges_duplicate_delivery() -> None:
    repository, publisher, account_id, capture_id = stored_capture()
    event = publisher.events[0]
    app = create_image_worker_app(worker_settings(), repository=repository)
    envelope = push_envelope(event.model_dump(mode="json"))

    with TestClient(app) as client:
        health = client.get("/health")
        grouped = client.post("/internal/pubsub/capture-stored", json=envelope)
        event_revision = repository._captures[capture_id].event_id
        duplicate = client.post("/internal/pubsub/capture-stored", json=envelope)

    assert health.json() == {"status": "ok", "mode": "test"}
    assert grouped.status_code == 204
    assert duplicate.status_code == 204
    assert repository._captures[capture_id].event_id == event_revision
    assert repository._jobs[(account_id, capture_grouping_job_id(capture_id))].status == (
        JobStatus.COMPLETED
    )


def test_image_worker_rejects_bad_events_and_retries_processing_failures(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    repository, publisher, account_id, capture_id = stored_capture(owner_user_id="failure-owner")
    app = create_image_worker_app(worker_settings(), repository=repository)
    valid_envelope = push_envelope(publisher.events[0].model_dump(mode="json"))

    async def fail_grouping(**_):
        raise RuntimeError("simulated grouping failure")

    monkeypatch.setattr(repository, "group_capture", fail_grouping)
    with TestClient(app) as client:
        malformed = client.post(
            "/internal/pubsub/capture-stored",
            json=push_envelope({"kind": "wrong"}),
        )
        failed = client.post("/internal/pubsub/capture-stored", json=valid_envelope)

    job = repository._jobs[(account_id, capture_grouping_job_id(capture_id))]
    assert malformed.status_code == 400
    assert malformed.json() == {"detail": "invalid_pubsub_event"}
    assert failed.status_code == 503
    assert failed.json() == {"detail": "capture_grouping_failed"}
    assert job.status == JobStatus.PENDING
    assert job.last_error_code == "RuntimeError"
    output = capfd.readouterr().out
    assert '"event":"capture_grouping_failed"' in output
    assert '"error_kind":"RuntimeError"' in output
    assert account_id in output
    assert capture_id in output
    assert "simulated grouping failure" not in output
