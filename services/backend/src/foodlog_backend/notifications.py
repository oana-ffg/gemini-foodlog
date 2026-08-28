from contextlib import suppress
from datetime import timedelta
from typing import Literal, Protocol
from uuid import uuid4

import httpx
from google.cloud import pubsub_v1
from pydantic import BaseModel, ConfigDict

from .audit import record_audit_event
from .models import (
    Account,
    AccountCreatedOutbox,
    AuditAction,
    AuditActorKind,
    AuditSource,
    EntitlementMode,
    utc_now,
)
from .operational_logging import emit_operational_event
from .pubsub import PubSubJsonPublisher
from .repository import Repository

OUTBOX_LEASE_DURATION = timedelta(minutes=5)


class AccountCreatedEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["account_created"] = "account_created"
    event_id: str


class NotificationPublisher(Protocol):
    async def publish(self, event: AccountCreatedEventV1) -> str: ...


class PushoverSender(Protocol):
    async def send_account_created(self, event: AccountCreatedOutbox) -> str: ...


class InMemoryNotificationPublisher:
    def __init__(self) -> None:
        self.events: list[AccountCreatedEventV1] = []
        self.failure: Exception | None = None

    async def publish(self, event: AccountCreatedEventV1) -> str:
        if self.failure is not None:
            raise self.failure
        self.events.append(event.model_copy(deep=True))
        return f"memory-message-{len(self.events)}"


class PubSubNotificationPublisher:
    def __init__(
        self,
        *,
        topic: str,
        client: pubsub_v1.PublisherClient | None = None,
    ) -> None:
        self._publisher = PubSubJsonPublisher(topic=topic, client=client)

    async def publish(self, event: AccountCreatedEventV1) -> str:
        return await self._publisher.publish(
            event,
            event_kind=event.kind,
            schema_version=event.schema_version,
        )


class AccountProvisioningService:
    def __init__(
        self,
        *,
        repository: Repository,
        publisher: NotificationPublisher,
        public_account_limit: int = 25,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._public_account_limit = public_account_limit

    async def provision_account(
        self,
        owner_user_id: str,
        *,
        verified_email_normalized: str | None = None,
    ) -> Account:
        account = await self._repository.provision_account(
            owner_user_id,
            verified_email_normalized=verified_email_normalized,
        )
        await record_audit_event(
            self._repository,
            account_id=account.id,
            action=AuditAction.ACCOUNT_PROVISIONED,
            actor_kind=AuditActorKind.USER,
            source=AuditSource.API,
            subject_kind="account",
            subject_id=account.id,
        )
        lease_id = str(uuid4())
        event: AccountCreatedOutbox | None = None
        try:
            event = await self._repository.claim_account_notification_for_publish(
                account_id=account.id,
                lease_id=lease_id,
                lease_expires_at=utc_now() + OUTBOX_LEASE_DURATION,
            )
            if event is None:
                return account
            if event.publish_attempt_count == 1 and event.public_slot_number is not None:
                emit_operational_event(
                    "INFO",
                    "account_capacity_observed",
                    service="api",
                    account_id=account.id,
                    account_capacity_count=event.public_slot_number,
                    account_capacity_limit=self._public_account_limit,
                    outcome="account_created",
                )
            provider_message_id = await self._publisher.publish(
                AccountCreatedEventV1(event_id=event.id)
            )
            await self._repository.mark_account_notification_published(
                event_id=event.id,
                lease_id=lease_id,
                provider_message_id=provider_message_id,
            )
        except Exception as error:
            if event is not None:
                with suppress(Exception):
                    await self._repository.release_account_notification_publish(
                        event_id=event.id,
                        lease_id=lease_id,
                        error_code=type(error).__name__[:120],
                    )
        return account


class PushoverDeliveryError(RuntimeError):
    pass


class PushoverClient:
    endpoint = "https://api.pushover.net/1/messages.json"

    def __init__(
        self,
        *,
        app_token: str,
        user_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_token = app_token
        self._user_key = user_key
        self._client = client

    @staticmethod
    def message(event: AccountCreatedOutbox) -> str:
        if event.entitlement_mode == EntitlementMode.TRIAL:
            entitlement = f"Trial: {event.trial_image_limit} images"
            slot = f"Public slot: {event.public_slot_number}/25"
        else:
            entitlement = "Entitlement: unlimited"
            slot = "Account class: internal"
        return "\n".join(
            [
                f"Event: {event.id}",
                f"Account: {event.account_id}",
                slot,
                entitlement,
                f"Created: {event.created_at.isoformat()}",
            ]
        )

    async def _send(self, client: httpx.AsyncClient, event: AccountCreatedOutbox) -> str:
        response = await client.post(
            self.endpoint,
            data={
                "token": self._app_token,
                "user": self._user_key,
                "title": "FoodLog account created",
                "message": self.message(event),
                "priority": "0",
            },
        )
        try:
            body = response.json()
        except ValueError as error:
            raise PushoverDeliveryError("pushover_invalid_response") from error
        if response.status_code != 200 or body.get("status") != 1:
            raise PushoverDeliveryError("pushover_rejected_message")
        request_id = body.get("request")
        if not isinstance(request_id, str) or not request_id:
            raise PushoverDeliveryError("pushover_missing_request_id")
        return request_id

    async def send_account_created(self, event: AccountCreatedOutbox) -> str:
        if self._client is not None:
            return await self._send(self._client, event)
        async with httpx.AsyncClient(timeout=10) as client:
            return await self._send(client, event)


class AccountNotificationService:
    def __init__(
        self,
        *,
        repository: Repository,
        sender: PushoverSender,
    ) -> None:
        self._repository = repository
        self._sender = sender

    async def deliver(self, event_id: str) -> bool:
        lease_id = str(uuid4())
        event = await self._repository.claim_account_notification_for_delivery(
            event_id=event_id,
            lease_id=lease_id,
            lease_expires_at=utc_now() + OUTBOX_LEASE_DURATION,
        )
        if event is None:
            return False
        try:
            provider_delivery_id = await self._sender.send_account_created(event)
        except Exception as error:
            await self._repository.release_account_notification_delivery(
                event_id=event.id,
                lease_id=lease_id,
                error_code=type(error).__name__[:120],
            )
            raise
        marked = await self._repository.mark_account_notification_delivered(
            event_id=event.id,
            lease_id=lease_id,
            provider_delivery_id=provider_delivery_id,
        )
        if not marked:
            raise RuntimeError("account notification delivery lease was lost")
        return True
