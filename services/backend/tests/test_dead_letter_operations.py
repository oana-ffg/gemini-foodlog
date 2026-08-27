import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from foodlog_backend.dead_letter_operations import (
    DeadLetterOperationsService,
    DeadLetterStream,
)
from foodlog_backend.models import (
    AuditAction,
    AuditPurpose,
    CaptureRecord,
    CaptureStatus,
    DurableJob,
    JobKind,
    JobStatus,
    capture_grouping_job_id,
)


def received_message(
    *,
    message_id: str,
    payload: dict,
    ack_id: str | None = None,
    delivery_attempt: int = 1,
    source_subscription: str = "foodlog-image-consumer",
):
    return SimpleNamespace(
        ack_id=ack_id or f"ack-{message_id}",
        delivery_attempt=delivery_attempt,
        message=SimpleNamespace(
            message_id=message_id,
            data=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
            publish_time=datetime(2026, 8, 25, 22, 33, tzinfo=UTC),
            attributes={
                "CloudPubSubDeadLetterSourceDeliveryCount": "5",
                "CloudPubSubDeadLetterSourceSubscription": source_subscription,
                "CloudPubSubDeadLetterSourceSubscriptionProject": "foodlog-test-2026",
            },
        ),
    )


class FakeSubscriber:
    def __init__(self, messages):
        self.messages = messages
        self.pull_requests: list[dict] = []
        self.deadline_requests: list[dict] = []
        self.ack_requests: list[dict] = []

    def pull(self, *, request, timeout=None):
        self.pull_requests.append({"request": request, "timeout": timeout})
        return SimpleNamespace(received_messages=self.messages)

    def modify_ack_deadline(self, *, request):
        self.deadline_requests.append(request)

    def acknowledge(self, *, request):
        self.ack_requests.append(request)


class FakeRepository:
    def __init__(self):
        self.audits = []
        self.failure: Exception | None = None
        self.captures = {}
        self.jobs = {}

    async def append_audit_event(self, event):
        if self.failure is not None:
            raise self.failure
        self.audits.append(event)
        return event

    async def capture_for_account(self, *, account_id, capture_id):
        return self.captures[(account_id, capture_id)]

    async def job_for_account(self, account_id, job_id):
        return self.jobs.get((account_id, job_id))


class FakePublisher:
    def __init__(self):
        self.events = []
        self.failure: Exception | None = None

    async def publish(self, event, *, event_kind, schema_version):
        if self.failure is not None:
            raise self.failure
        self.events.append((event, event_kind, schema_version))
        return "replay-message-9001"


def image_payload(capture_id: str = "capture-1") -> dict:
    return {
        "schema_version": 1,
        "kind": "capture_stored",
        "account_id": "account-1",
        "capture_id": capture_id,
    }


def test_inspection_audits_metadata_and_immediately_releases_without_ack() -> None:
    subscriber = FakeSubscriber([received_message(message_id="message-1", payload=image_payload())])
    repository = FakeRepository()
    service = DeadLetterOperationsService(
        project_id="foodlog-test-2026",
        repository=repository,
        subscriber=subscriber,
    )

    result = asyncio.run(
        service.inspect(
            stream=DeadLetterStream.IMAGE,
            purpose=AuditPurpose.INCIDENT_TRIAGE,
            session_id="session-1",
        )
    )

    assert result.subscription.endswith("/foodlog-image-dead-letter-inspection")
    assert result.messages[0].model_dump() == {
        "stream": DeadLetterStream.IMAGE,
        "message_id": "message-1",
        "published_at": datetime(2026, 8, 25, 22, 33, tzinfo=UTC),
        "delivery_attempt": 1,
        "account_id": "account-1",
        "subject_kind": "capture",
        "subject_id": "capture-1",
        "event_kind": "capture_stored",
        "schema_version": 1,
        "source_subscription": (
            "projects/foodlog-test-2026/subscriptions/foodlog-image-consumer"
        ),
        "source_delivery_count": 5,
    }
    assert [audit.action for audit in repository.audits] == [
        AuditAction.OPERATOR_DEAD_LETTER_INSPECTED
    ]
    assert subscriber.deadline_requests == [
        {
            "subscription": result.subscription,
            "ack_ids": ["ack-message-1"],
            "ack_deadline_seconds": 0,
        }
    ]
    assert subscriber.ack_requests == []
    assert "ack-message-1" not in result.model_dump_json()


def test_invalid_payload_fails_closed_and_is_released() -> None:
    bad = image_payload()
    bad["unexpected"] = "private-payload"
    subscriber = FakeSubscriber([received_message(message_id="message-2", payload=bad)])
    service = DeadLetterOperationsService(
        project_id="foodlog-test-2026",
        repository=FakeRepository(),
        subscriber=subscriber,
    )

    with pytest.raises(ValueError, match="stream schema"):
        asyncio.run(
            service.inspect(
                stream=DeadLetterStream.IMAGE,
                purpose=AuditPurpose.INCIDENT_TRIAGE,
                session_id="session-2",
            )
        )

    assert subscriber.deadline_requests[0]["ack_ids"] == ["ack-message-2"]
    assert subscriber.ack_requests == []


def test_replay_requires_exact_confirmation_before_pull() -> None:
    subscriber = FakeSubscriber([])
    service = DeadLetterOperationsService(
        project_id="foodlog-test-2026",
        repository=FakeRepository(),
        subscriber=subscriber,
    )

    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(
            service.replay(
                stream=DeadLetterStream.IMAGE,
                message_id="message-1",
                confirmed_message_id="message-2",
                purpose=AuditPurpose.INCIDENT_TRIAGE,
                session_id="session-3",
            )
        )

    assert subscriber.pull_requests == []


def test_replay_releases_non_targets_and_acks_target_only_after_publish_and_audit() -> None:
    subscriber = FakeSubscriber(
        [
            received_message(message_id="other-message", payload=image_payload("capture-other")),
            received_message(message_id="target-message", payload=image_payload("capture-target")),
        ]
    )
    repository = FakeRepository()
    publisher = FakePublisher()
    service = DeadLetterOperationsService(
        project_id="foodlog-test-2026",
        repository=repository,
        subscriber=subscriber,
        publisher=publisher,
    )

    result = asyncio.run(
        service.replay(
            stream=DeadLetterStream.IMAGE,
            message_id="target-message",
            confirmed_message_id="target-message",
            purpose=AuditPurpose.INCIDENT_TRIAGE,
            session_id="session-4",
        )
    )

    assert result.topic.endswith("/foodlog-image-events")
    assert result.replay_message_id == "replay-message-9001"
    assert [item.action for item in repository.audits] == [
        AuditAction.OPERATOR_DEAD_LETTER_REPLAY_REQUESTED,
        AuditAction.OPERATOR_DEAD_LETTER_REPLAY_PUBLISHED,
    ]
    [(event, kind, version)] = publisher.events
    assert event.capture_id == "capture-target"
    assert (kind, version) == ("capture_stored", 1)
    assert subscriber.deadline_requests == [
        {
            "subscription": result.subscription,
            "ack_ids": ["ack-other-message"],
            "ack_deadline_seconds": 0,
        }
    ]
    assert subscriber.ack_requests == [
        {"subscription": result.subscription, "ack_ids": ["ack-target-message"]}
    ]


def test_failed_replay_is_not_acked_and_is_immediately_released() -> None:
    subscriber = FakeSubscriber(
        [received_message(message_id="target-message", payload=image_payload())]
    )
    publisher = FakePublisher()
    publisher.failure = RuntimeError("simulated publication failure")
    service = DeadLetterOperationsService(
        project_id="foodlog-test-2026",
        repository=FakeRepository(),
        subscriber=subscriber,
        publisher=publisher,
    )

    with pytest.raises(RuntimeError, match="publication failure"):
        asyncio.run(
            service.replay(
                stream=DeadLetterStream.IMAGE,
                message_id="target-message",
                confirmed_message_id="target-message",
                purpose=AuditPurpose.INCIDENT_TRIAGE,
                session_id="session-5",
            )
        )

    assert subscriber.ack_requests == []
    assert subscriber.deadline_requests[-1]["ack_ids"] == ["ack-target-message"]


def test_notification_identity_is_derived_from_the_validated_outbox_event() -> None:
    subscriber = FakeSubscriber(
        [
            received_message(
                message_id="notification-message",
                payload={
                    "schema_version": 1,
                    "kind": "account_created",
                    "event_id": "account-created-account-9",
                },
                source_subscription="foodlog-notification-consumer",
            )
        ]
    )
    service = DeadLetterOperationsService(
        project_id="foodlog-test-2026",
        repository=FakeRepository(),
        subscriber=subscriber,
    )

    result = asyncio.run(
        service.inspect(
            stream=DeadLetterStream.NOTIFICATION,
            purpose=AuditPurpose.SUPPORT,
            session_id="session-6",
        )
    )

    assert result.messages[0].account_id == "account-9"
    assert result.messages[0].subject_kind == "account_notification"


def test_acknowledge_resolved_image_requires_completed_source_job() -> None:
    subscriber = FakeSubscriber(
        [received_message(message_id="resolved-message", payload=image_payload())]
    )
    repository = FakeRepository()
    repository.captures[("account-1", "capture-1")] = CaptureRecord(
        id="capture-1",
        account_id="account-1",
        camera_id="camera-1",
        idempotency_key="idempotency-1",
        content_type="image/png",
        content_sha256="a" * 64,
        object_key="accounts/account-1/captures/capture-1.png",
        status=CaptureStatus.STORED,
    )
    job_id = capture_grouping_job_id("capture-1")
    repository.jobs[("account-1", job_id)] = DurableJob(
        id=job_id,
        account_id="account-1",
        kind=JobKind.CAPTURE_GROUPING,
        subject_id="capture-1",
        subject_revision=1,
        status=JobStatus.COMPLETED,
        completed_at=datetime(2026, 8, 25, 23, tzinfo=UTC),
    )
    service = DeadLetterOperationsService(
        project_id="foodlog-test-2026",
        repository=repository,
        subscriber=subscriber,
    )

    result = asyncio.run(
        service.acknowledge_resolved_image(
            message_id="resolved-message",
            confirmed_message_id="resolved-message",
            confirmed_capture_id="capture-1",
            purpose=AuditPurpose.INCIDENT_TRIAGE,
            session_id="session-7",
        )
    )

    assert result.durable_job_id == job_id
    assert result.durable_job_status == "completed"
    assert [audit.action for audit in repository.audits] == [
        AuditAction.OPERATOR_DEAD_LETTER_RESOLUTION_REQUESTED,
        AuditAction.OPERATOR_DEAD_LETTER_RESOLVED_ACKNOWLEDGED,
    ]
    assert subscriber.ack_requests[0]["ack_ids"] == ["ack-resolved-message"]


def test_acknowledge_resolved_image_refuses_pending_job_and_releases_message() -> None:
    subscriber = FakeSubscriber(
        [received_message(message_id="pending-message", payload=image_payload())]
    )
    repository = FakeRepository()
    repository.captures[("account-1", "capture-1")] = CaptureRecord(
        id="capture-1",
        account_id="account-1",
        camera_id="camera-1",
        idempotency_key="idempotency-1",
        content_type="image/png",
        content_sha256="a" * 64,
        object_key="accounts/account-1/captures/capture-1.png",
        status=CaptureStatus.STORED,
    )
    job_id = capture_grouping_job_id("capture-1")
    repository.jobs[("account-1", job_id)] = DurableJob(
        id=job_id,
        account_id="account-1",
        kind=JobKind.CAPTURE_GROUPING,
        subject_id="capture-1",
        subject_revision=1,
    )
    service = DeadLetterOperationsService(
        project_id="foodlog-test-2026",
        repository=repository,
        subscriber=subscriber,
    )

    with pytest.raises(ValueError, match="not proven completed"):
        asyncio.run(
            service.acknowledge_resolved_image(
                message_id="pending-message",
                confirmed_message_id="pending-message",
                confirmed_capture_id="capture-1",
                purpose=AuditPurpose.INCIDENT_TRIAGE,
                session_id="session-8",
            )
        )

    assert subscriber.ack_requests == []
    assert subscriber.deadline_requests[-1]["ack_ids"] == ["ack-pending-message"]
