import asyncio
import json
from base64 import b64encode
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from foodlog_backend.models import NotificationOutboxStatus
from foodlog_backend.notification_app import NotificationSettings, create_notification_app
from foodlog_backend.notifications import (
    AccountNotificationService,
    AccountProvisioningService,
    InMemoryNotificationPublisher,
    PushoverClient,
)
from foodlog_backend.repository import InMemoryRepository


class FakePushoverSender:
    def __init__(self) -> None:
        self.events = []
        self.failure: Exception | None = None

    async def send_account_created(self, event):
        if self.failure is not None:
            raise self.failure
        self.events.append(event.model_copy(deep=True))
        return f"pushover-request-{len(self.events)}"


def build_repository() -> InMemoryRepository:
    return InMemoryRepository(public_account_limit=25, trial_image_limit=200)


def provision_published_account(
    repository: InMemoryRepository,
) -> tuple[str, str]:
    publisher = InMemoryNotificationPublisher()
    service = AccountProvisioningService(repository=repository, publisher=publisher)
    account = asyncio.run(service.provision_account("notification-owner"))
    return account.id, publisher.events[0].event_id


def test_account_provisioning_creates_and_publishes_one_durable_outbox_event(
    capfd: pytest.CaptureFixture[str],
) -> None:
    repository = build_repository()
    publisher = InMemoryNotificationPublisher()
    service = AccountProvisioningService(repository=repository, publisher=publisher)

    created = asyncio.run(service.provision_account("owner-a"))
    retry = asyncio.run(service.provision_account("owner-a"))
    event = repository._notification_outbox[f"account-created-{created.id}"]

    assert retry.id == created.id
    assert len(publisher.events) == 1
    assert publisher.events[0].model_dump() == {
        "schema_version": 1,
        "kind": "account_created",
        "event_id": event.id,
    }
    assert event.account_id == created.id
    assert event.public_slot_number == 1
    assert event.trial_image_limit == 200
    assert event.status == NotificationOutboxStatus.PUBLISHED
    assert event.publish_attempt_count == 1
    assert event.provider_message_id == "memory-message-1"
    [capacity_event] = [
        json.loads(line)
        for line in capfd.readouterr().out.splitlines()
        if '"event":"account_capacity_observed"' in line
    ]
    assert capacity_event["service"] == "api"
    assert capacity_event["account_capacity_count"] == 1
    assert capacity_event["account_capacity_limit"] == 25
    assert capacity_event["outcome"] == "account_created"


def test_pubsub_outage_never_rolls_back_signup_and_retry_recovers() -> None:
    repository = build_repository()
    publisher = InMemoryNotificationPublisher()
    publisher.failure = RuntimeError("simulated Pub/Sub outage")
    service = AccountProvisioningService(repository=repository, publisher=publisher)

    account = asyncio.run(service.provision_account("owner-a"))
    event = repository._notification_outbox[f"account-created-{account.id}"]

    assert account.id in repository._accounts
    assert event.status == NotificationOutboxStatus.PENDING
    assert event.publish_attempt_count == 1
    assert event.last_error_code == "RuntimeError"

    publisher.failure = None
    retry = asyncio.run(service.provision_account("owner-a"))

    assert retry.id == account.id
    assert event.status == NotificationOutboxStatus.PUBLISHED
    assert event.publish_attempt_count == 2
    assert len(publisher.events) == 1


def test_notification_delivery_is_idempotent_for_duplicate_event_ids() -> None:
    repository = build_repository()
    _, event_id = provision_published_account(repository)
    sender = FakePushoverSender()
    service = AccountNotificationService(repository=repository, sender=sender)

    delivered = asyncio.run(service.deliver(event_id))
    duplicate = asyncio.run(service.deliver(event_id))
    event = repository._notification_outbox[event_id]

    assert delivered is True
    assert duplicate is False
    assert len(sender.events) == 1
    assert event.status == NotificationOutboxStatus.DELIVERED
    assert event.delivery_attempt_count == 1
    assert event.provider_delivery_id == "pushover-request-1"


def test_notification_delivery_outage_releases_the_lease_for_retry() -> None:
    repository = build_repository()
    _, event_id = provision_published_account(repository)
    sender = FakePushoverSender()
    sender.failure = RuntimeError("simulated Pushover outage")
    service = AccountNotificationService(repository=repository, sender=sender)

    try:
        asyncio.run(service.deliver(event_id))
    except RuntimeError as error:
        assert str(error) == "simulated Pushover outage"
    else:
        raise AssertionError("delivery outage should be observable to Pub/Sub")

    event = repository._notification_outbox[event_id]
    assert event.status == NotificationOutboxStatus.PUBLISHED
    assert event.delivery_attempt_count == 1
    assert event.last_error_code == "RuntimeError"

    sender.failure = None
    assert asyncio.run(service.deliver(event_id)) is True
    assert event.status == NotificationOutboxStatus.DELIVERED
    assert event.delivery_attempt_count == 2


def test_pushover_request_contains_only_bounded_operational_account_data() -> None:
    repository = build_repository()
    account_id, event_id = provision_published_account(repository)
    event = repository._notification_outbox[event_id]
    observed_form: dict[str, list[str]] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed_form.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"status": 1, "request": "request-id-123"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = PushoverClient(
        app_token="test-application-token-000000000",
        user_key="test-recipient-key-000000000000",
        client=client,
    )

    request_id = asyncio.run(sender.send_account_created(event))
    asyncio.run(client.aclose())

    assert request_id == "request-id-123"
    assert observed_form["priority"] == ["0"]
    assert observed_form["title"] == ["FoodLog account created"]
    message = observed_form["message"][0]
    assert event_id in message
    assert account_id in message
    assert "Public slot: 1/25" in message
    assert "Trial: 200 images" in message
    assert "notification-owner" not in message
    assert "food" not in message.lower()


def test_notification_worker_decodes_pubsub_and_retries_provider_failures() -> None:
    repository = build_repository()
    _, event_id = provision_published_account(repository)
    sender = FakePushoverSender()
    settings = NotificationSettings(
        environment="test",
        gcp_project_id="test-project",
        pushover_app_token="test-application-token-000000000",
        pushover_user_key="test-recipient-key-000000000000",
    )
    app = create_notification_app(settings, repository=repository, sender=sender)
    payload = b64encode(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "account_created",
                "event_id": event_id,
            }
        ).encode()
    ).decode()
    envelope = {
        "message": {"data": payload, "messageId": "pubsub-message-1"},
        "subscription": "foodlog-notification-consumer",
    }

    with TestClient(app) as client:
        delivered = client.post("/internal/pubsub/account-created", json=envelope)
        duplicate = client.post("/internal/pubsub/account-created", json=envelope)
        invalid = client.post(
            "/internal/pubsub/account-created",
            json={
                "message": {"data": "not-base64", "messageId": "bad"},
                "subscription": "foodlog-notification-consumer",
            },
        )

    assert delivered.status_code == duplicate.status_code == 204
    assert invalid.status_code == 400
    assert len(sender.events) == 1
