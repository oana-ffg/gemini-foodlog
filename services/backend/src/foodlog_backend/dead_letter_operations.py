from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from google.api_core.exceptions import DeadlineExceeded
from google.cloud import pubsub_v1
from pydantic import BaseModel, ConfigDict, ValidationError

from .audit import build_audit_event
from .image_events import CaptureStoredEventV1
from .mail_events import RawMailStoredEventV1
from .models import (
    AuditAction,
    AuditActorKind,
    AuditEvent,
    AuditPurpose,
    AuditSource,
    CaptureRecord,
    DurableJob,
    JobKind,
    JobStatus,
    capture_grouping_job_id,
    event_inference_job_id,
)
from .notifications import AccountCreatedEventV1
from .pubsub import PubSubJsonPublisher

MAX_DEAD_LETTER_MESSAGES = 10


class DeadLetterStream(StrEnum):
    IMAGE = "image"
    MAIL = "mail"
    NOTIFICATION = "notification"


class DeadLetterRepository(Protocol):
    async def append_audit_event(self, event: AuditEvent) -> AuditEvent: ...

    async def capture_for_account(
        self,
        *,
        account_id: str,
        capture_id: str,
    ) -> CaptureRecord: ...

    async def job_for_account(self, account_id: str, job_id: str) -> DurableJob | None: ...


class DeadLetterSubscriber(Protocol):
    def pull(self, *, request: Mapping[str, Any], timeout: float | None = None) -> Any: ...

    def modify_ack_deadline(self, *, request: Mapping[str, Any]) -> Any: ...

    def acknowledge(self, *, request: Mapping[str, Any]) -> Any: ...


class DeadLetterPublisher(Protocol):
    async def publish(
        self,
        event: BaseModel,
        *,
        event_kind: str,
        schema_version: int,
    ) -> str: ...


class DeadLetterModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeadLetterMessageMetadata(DeadLetterModel):
    stream: DeadLetterStream
    message_id: str
    published_at: datetime | None
    delivery_attempt: int
    account_id: str
    subject_kind: str
    subject_id: str
    event_kind: str
    schema_version: int
    source_subscription: str
    source_delivery_count: int


class DeadLetterInspectionResult(DeadLetterModel):
    schema_version: str = "foodlog-dead-letter-inspection-v1"
    subscription: str
    messages: list[DeadLetterMessageMetadata]
    audit_event_ids: list[str]


class DeadLetterReplayResult(DeadLetterModel):
    schema_version: str = "foodlog-dead-letter-replay-v1"
    subscription: str
    topic: str
    source_message_id: str
    replay_message_id: str
    account_id: str
    subject_kind: str
    subject_id: str
    request_audit_event_id: str
    published_audit_event_id: str
    source_acknowledged: bool = True


class DeadLetterResolutionResult(DeadLetterModel):
    schema_version: str = "foodlog-dead-letter-resolution-v1"
    subscription: str
    source_message_id: str
    source_subscription: str
    account_id: str
    subject_kind: str
    subject_id: str
    durable_job_id: str
    durable_job_status: str
    request_audit_event_id: str
    acknowledged_audit_event_id: str
    source_acknowledged: bool = True


@dataclass(frozen=True, slots=True)
class _PulledMessage:
    ack_id: str
    metadata: DeadLetterMessageMetadata
    event: BaseModel


def expected_subscription(project_id: str, stream: DeadLetterStream) -> str:
    return (
        f"projects/{project_id}/subscriptions/"
        f"foodlog-{stream.value}-dead-letter-inspection"
    )


def expected_topic(project_id: str, stream: DeadLetterStream) -> str:
    return f"projects/{project_id}/topics/foodlog-{stream.value}-events"


def expected_source_subscriptions(
    project_id: str,
    stream: DeadLetterStream,
) -> frozenset[str]:
    prefix = f"projects/{project_id}/subscriptions/"
    if stream == DeadLetterStream.IMAGE:
        return frozenset(
            {
                f"{prefix}foodlog-image-consumer",
                f"{prefix}foodlog-inference-consumer",
            }
        )
    return frozenset({f"{prefix}foodlog-{stream.value}-consumer"})


def _event_identity(stream: DeadLetterStream, event: BaseModel) -> tuple[str, str, str]:
    if stream == DeadLetterStream.IMAGE:
        assert isinstance(event, CaptureStoredEventV1)
        return event.account_id, "capture", event.capture_id
    if stream == DeadLetterStream.MAIL:
        assert isinstance(event, RawMailStoredEventV1)
        return event.account_id, "raw_mail", event.mail_id
    assert isinstance(event, AccountCreatedEventV1)
    prefix = "account-created-"
    if not event.event_id.startswith(prefix) or len(event.event_id) <= len(prefix):
        raise ValueError("notification dead-letter event has no account identity")
    return event.event_id[len(prefix) :], "account_notification", event.event_id


def _decode_message(
    project_id: str,
    stream: DeadLetterStream,
    received: Any,
) -> _PulledMessage:
    ack_id = getattr(received, "ack_id", "")
    message = getattr(received, "message", None)
    message_id = getattr(message, "message_id", "")
    if not isinstance(ack_id, str) or not ack_id:
        raise ValueError("dead-letter delivery has no acknowledgement identity")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("dead-letter delivery has no message identity")
    attributes = getattr(message, "attributes", None)
    if not isinstance(attributes, Mapping):
        raise ValueError("dead-letter delivery has no source attributes")
    source_subscription_value = attributes.get("CloudPubSubDeadLetterSourceSubscription")
    if not isinstance(source_subscription_value, str):
        raise ValueError("dead-letter source subscription is invalid")
    if source_subscription_value.startswith("projects/"):
        source_subscription = source_subscription_value
    else:
        source_subscription = (
            f"projects/{project_id}/subscriptions/{source_subscription_value}"
        )
    if source_subscription not in expected_source_subscriptions(project_id, stream):
        raise ValueError("dead-letter source subscription escaped its configured stream")
    source_project = attributes.get("CloudPubSubDeadLetterSourceSubscriptionProject")
    if source_project not in {project_id, f"projects/{project_id}"}:
        raise ValueError("dead-letter source project escaped its configured project")
    try:
        source_delivery_count = int(
            attributes["CloudPubSubDeadLetterSourceDeliveryCount"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("dead-letter source delivery count is invalid") from error
    if not 1 <= source_delivery_count <= 100:
        raise ValueError("dead-letter source delivery count is outside Pub/Sub bounds")
    data = getattr(message, "data", None)
    if not isinstance(data, bytes):
        raise ValueError("dead-letter payload is not bytes")
    try:
        decoded = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("dead-letter payload is not JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError("dead-letter payload is not a JSON object")
    event_type: type[BaseModel]
    if stream == DeadLetterStream.IMAGE:
        event_type = CaptureStoredEventV1
    elif stream == DeadLetterStream.MAIL:
        event_type = RawMailStoredEventV1
    else:
        event_type = AccountCreatedEventV1
    try:
        event = event_type.model_validate(decoded)
    except ValidationError as error:
        raise ValueError("dead-letter payload does not match its stream schema") from error
    account_id, subject_kind, subject_id = _event_identity(stream, event)
    delivery_attempt = getattr(received, "delivery_attempt", 0)
    if isinstance(delivery_attempt, bool) or not isinstance(delivery_attempt, int):
        raise ValueError("dead-letter delivery attempt is invalid")
    publish_time = getattr(message, "publish_time", None)
    return _PulledMessage(
        ack_id=ack_id,
        event=event,
        metadata=DeadLetterMessageMetadata(
            stream=stream,
            message_id=message_id,
            published_at=publish_time if isinstance(publish_time, datetime) else None,
            delivery_attempt=max(delivery_attempt, 0),
            account_id=account_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            event_kind=str(decoded["kind"]),
            schema_version=int(decoded["schema_version"]),
            source_subscription=source_subscription,
            source_delivery_count=source_delivery_count,
        ),
    )


class DeadLetterOperationsService:
    def __init__(
        self,
        *,
        project_id: str,
        repository: DeadLetterRepository,
        subscriber: DeadLetterSubscriber | None = None,
        publisher: DeadLetterPublisher | None = None,
    ) -> None:
        self._project_id = project_id
        self._repository = repository
        self._subscriber = subscriber or pubsub_v1.SubscriberClient()
        self._publisher = publisher

    async def _pull(
        self,
        *,
        stream: DeadLetterStream,
        max_messages: int,
    ) -> tuple[str, list[_PulledMessage]]:
        if not 1 <= max_messages <= MAX_DEAD_LETTER_MESSAGES:
            raise ValueError("dead-letter pull must request between 1 and 10 messages")
        subscription = expected_subscription(self._project_id, stream)
        try:
            response = await asyncio.to_thread(
                self._subscriber.pull,
                request={"subscription": subscription, "max_messages": max_messages},
                timeout=15.0,
            )
        except DeadlineExceeded:
            return subscription, []
        received_messages = list(getattr(response, "received_messages", ()))
        try:
            pulled = [
                _decode_message(self._project_id, stream, item)
                for item in received_messages
            ]
        except Exception:
            await self._release(subscription, received_messages)
            raise
        return subscription, pulled

    async def _release(self, subscription: str, messages: list[Any]) -> None:
        ack_ids = [getattr(message, "ack_id", "") for message in messages]
        ack_ids = [ack_id for ack_id in ack_ids if isinstance(ack_id, str) and ack_id]
        if ack_ids:
            await asyncio.to_thread(
                self._subscriber.modify_ack_deadline,
                request={
                    "subscription": subscription,
                    "ack_ids": ack_ids,
                    "ack_deadline_seconds": 0,
                },
            )

    async def inspect(
        self,
        *,
        stream: DeadLetterStream,
        purpose: AuditPurpose,
        session_id: str,
        max_messages: int = MAX_DEAD_LETTER_MESSAGES,
    ) -> DeadLetterInspectionResult:
        subscription, pulled = await self._pull(stream=stream, max_messages=max_messages)
        audits: list[AuditEvent] = []
        try:
            for item in pulled:
                metadata = item.metadata
                audits.append(
                    await self._repository.append_audit_event(
                        build_audit_event(
                            account_id=metadata.account_id,
                            action=AuditAction.OPERATOR_DEAD_LETTER_INSPECTED,
                            actor_kind=AuditActorKind.OPERATOR,
                            source=AuditSource.OPERATOR_CLI,
                            subject_kind=metadata.subject_kind,
                            subject_id=metadata.subject_id,
                            purpose=purpose,
                            occurrence_id=f"{session_id}:{metadata.message_id}",
                        )
                    )
                )
            return DeadLetterInspectionResult(
                subscription=subscription,
                messages=[item.metadata for item in pulled],
                audit_event_ids=[audit.id for audit in audits],
            )
        finally:
            await self._release(subscription, pulled)

    async def replay(
        self,
        *,
        stream: DeadLetterStream,
        message_id: str,
        confirmed_message_id: str,
        purpose: AuditPurpose,
        session_id: str,
    ) -> DeadLetterReplayResult:
        if message_id != confirmed_message_id:
            raise ValueError("confirmed dead-letter message ID does not match")
        subscription, pulled = await self._pull(
            stream=stream,
            max_messages=MAX_DEAD_LETTER_MESSAGES,
        )
        target = next((item for item in pulled if item.metadata.message_id == message_id), None)
        if target is None:
            await self._release(subscription, pulled)
            raise ValueError("confirmed dead-letter message was not in the bounded pull")
        non_targets = [item for item in pulled if item is not target]
        await self._release(subscription, non_targets)
        metadata = target.metadata
        topic = expected_topic(self._project_id, stream)
        publisher = self._publisher or PubSubJsonPublisher(topic=topic)
        try:
            request_audit = await self._repository.append_audit_event(
                build_audit_event(
                    account_id=metadata.account_id,
                    action=AuditAction.OPERATOR_DEAD_LETTER_REPLAY_REQUESTED,
                    actor_kind=AuditActorKind.OPERATOR,
                    source=AuditSource.OPERATOR_CLI,
                    subject_kind=metadata.subject_kind,
                    subject_id=metadata.subject_id,
                    purpose=purpose,
                    occurrence_id=f"{session_id}:{message_id}",
                )
            )
            replay_message_id = await publisher.publish(
                target.event,
                event_kind=metadata.event_kind,
                schema_version=metadata.schema_version,
            )
            published_audit = await self._repository.append_audit_event(
                build_audit_event(
                    account_id=metadata.account_id,
                    action=AuditAction.OPERATOR_DEAD_LETTER_REPLAY_PUBLISHED,
                    actor_kind=AuditActorKind.OPERATOR,
                    source=AuditSource.OPERATOR_CLI,
                    subject_kind=metadata.subject_kind,
                    subject_id=metadata.subject_id,
                    purpose=purpose,
                    occurrence_id=f"{session_id}:{message_id}:{replay_message_id}",
                )
            )
            await asyncio.to_thread(
                self._subscriber.acknowledge,
                request={"subscription": subscription, "ack_ids": [target.ack_id]},
            )
        except Exception:
            await self._release(subscription, [target])
            raise
        return DeadLetterReplayResult(
            subscription=subscription,
            topic=topic,
            source_message_id=message_id,
            replay_message_id=replay_message_id,
            account_id=metadata.account_id,
            subject_kind=metadata.subject_kind,
            subject_id=metadata.subject_id,
            request_audit_event_id=request_audit.id,
            published_audit_event_id=published_audit.id,
        )

    async def acknowledge_resolved_image(
        self,
        *,
        message_id: str,
        confirmed_message_id: str,
        confirmed_capture_id: str,
        purpose: AuditPurpose,
        session_id: str,
    ) -> DeadLetterResolutionResult:
        if message_id != confirmed_message_id:
            raise ValueError("confirmed dead-letter message ID does not match")
        subscription, pulled = await self._pull(
            stream=DeadLetterStream.IMAGE,
            max_messages=MAX_DEAD_LETTER_MESSAGES,
        )
        target = next((item for item in pulled if item.metadata.message_id == message_id), None)
        if target is None:
            await self._release(subscription, pulled)
            raise ValueError("confirmed dead-letter message was not in the bounded pull")
        non_targets = [item for item in pulled if item is not target]
        await self._release(subscription, non_targets)
        metadata = target.metadata
        if metadata.subject_id != confirmed_capture_id:
            await self._release(subscription, [target])
            raise ValueError("confirmed capture ID does not match the dead-letter payload")
        try:
            capture = await self._repository.capture_for_account(
                account_id=metadata.account_id,
                capture_id=metadata.subject_id,
            )
            grouping_subscription = (
                f"projects/{self._project_id}/subscriptions/foodlog-image-consumer"
            )
            if metadata.source_subscription == grouping_subscription:
                job_id = capture_grouping_job_id(capture.id)
                expected_kind = JobKind.CAPTURE_GROUPING
                expected_subject = capture.id
            else:
                if capture.event_id is None:
                    raise ValueError("capture has no grouped event for inference resolution")
                job_id = event_inference_job_id(capture.event_id)
                expected_kind = JobKind.EVENT_INFERENCE
                expected_subject = capture.event_id
            job = await self._repository.job_for_account(metadata.account_id, job_id)
            if (
                job is None
                or job.kind != expected_kind
                or job.subject_id != expected_subject
                or job.status != JobStatus.COMPLETED
            ):
                raise ValueError("dead-letter durable job is not proven completed")
            request_audit = await self._repository.append_audit_event(
                build_audit_event(
                    account_id=metadata.account_id,
                    action=AuditAction.OPERATOR_DEAD_LETTER_RESOLUTION_REQUESTED,
                    actor_kind=AuditActorKind.OPERATOR,
                    source=AuditSource.OPERATOR_CLI,
                    subject_kind=metadata.subject_kind,
                    subject_id=metadata.subject_id,
                    purpose=purpose,
                    occurrence_id=f"{session_id}:{message_id}:{job.id}",
                )
            )
            await asyncio.to_thread(
                self._subscriber.acknowledge,
                request={"subscription": subscription, "ack_ids": [target.ack_id]},
            )
            acknowledged_audit = await self._repository.append_audit_event(
                build_audit_event(
                    account_id=metadata.account_id,
                    action=AuditAction.OPERATOR_DEAD_LETTER_RESOLVED_ACKNOWLEDGED,
                    actor_kind=AuditActorKind.OPERATOR,
                    source=AuditSource.OPERATOR_CLI,
                    subject_kind=metadata.subject_kind,
                    subject_id=metadata.subject_id,
                    purpose=purpose,
                    occurrence_id=f"{session_id}:{message_id}:{job.id}",
                )
            )
        except Exception:
            await self._release(subscription, [target])
            raise
        return DeadLetterResolutionResult(
            subscription=subscription,
            source_message_id=message_id,
            source_subscription=metadata.source_subscription,
            account_id=metadata.account_id,
            subject_kind=metadata.subject_kind,
            subject_id=metadata.subject_id,
            durable_job_id=job.id,
            durable_job_status=job.status.value,
            request_audit_event_id=request_audit.id,
            acknowledged_audit_event_id=acknowledged_audit.id,
        )
