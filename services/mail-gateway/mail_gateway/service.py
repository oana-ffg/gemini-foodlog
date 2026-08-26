from __future__ import annotations

from hashlib import sha256
from typing import Protocol

from .domain import (
    MailIdentityCollision,
    RawMailRecord,
    RawMailStoredEventV1,
    UnknownRecipient,
    inspect_untrusted_mime,
    normalize_domain,
    normalize_recipient,
    recipient_hash,
    utc_now,
)


class MailRepository(Protocol):
    def resolve_recipient(self, *, recipient: str, recipient_hash: str) -> str: ...

    def reserve(self, record: RawMailRecord) -> RawMailRecord: ...

    def mark_stored(self, record: RawMailRecord) -> RawMailRecord: ...

    def mark_published(
        self, record: RawMailRecord, *, provider_message_id: str
    ) -> RawMailRecord: ...


class RawMailStore(Protocol):
    def put_if_absent(
        self,
        *,
        account_id: str,
        mail_id: str,
        object_key: str,
        content: bytes,
    ) -> None: ...


class MailEventPublisher(Protocol):
    def publish(self, event: RawMailStoredEventV1) -> str: ...


class MailGatewayService:
    def __init__(
        self,
        *,
        domain: str,
        repository: MailRepository,
        object_store: RawMailStore,
        event_publisher: MailEventPublisher,
    ) -> None:
        self._domain = normalize_domain(domain)
        self._repository = repository
        self._object_store = object_store
        self._event_publisher = event_publisher

    def receive(self, *, recipient: str, raw_message: bytes) -> RawMailRecord:
        normalized_recipient = normalize_recipient(recipient, expected_domain=self._domain)
        account_id = self._repository.resolve_recipient(
            recipient=normalized_recipient,
            recipient_hash=recipient_hash(
                normalized_recipient,
                expected_domain=self._domain,
            ),
        )
        if not account_id:
            raise UnknownRecipient

        content_hash = sha256(raw_message).hexdigest()
        inspection = inspect_untrusted_mime(raw_message)
        headers = inspection.headers
        identity_hash = headers.message_id_hash or content_hash
        record = self._record(
            account_id=account_id,
            recipient=normalized_recipient,
            raw_message=raw_message,
            content_hash=content_hash,
            identity_hash=identity_hash,
            message_id_hash=headers.message_id_hash,
            sender=headers.sender,
            sender_address=headers.sender_address,
            subject=headers.subject,
            mime_part_count=inspection.mime_part_count,
            attachment_count=inspection.attachment_count,
            content_types=inspection.content_types,
        )
        try:
            record = self._repository.reserve(record)
        except MailIdentityCollision:
            collision_safe_hash = sha256(f"{identity_hash}\0{content_hash}".encode()).hexdigest()
            record = self._repository.reserve(
                self._record(
                    account_id=account_id,
                    recipient=normalized_recipient,
                    raw_message=raw_message,
                    content_hash=content_hash,
                    identity_hash=collision_safe_hash,
                    message_id_hash=headers.message_id_hash,
                    sender=headers.sender,
                    sender_address=headers.sender_address,
                    subject=headers.subject,
                    mime_part_count=inspection.mime_part_count,
                    attachment_count=inspection.attachment_count,
                    content_types=inspection.content_types,
                )
            )

        if record.status == "published":
            return record
        self._object_store.put_if_absent(
            account_id=record.account_id,
            mail_id=record.id,
            object_key=record.object_key,
            content=raw_message,
        )
        if record.status == "reserved":
            record = self._repository.mark_stored(record)
        if record.status == "stored":
            provider_message_id = self._event_publisher.publish(
                RawMailStoredEventV1(
                    schema_version=1,
                    kind="raw_mail_stored",
                    account_id=record.account_id,
                    mail_id=record.id,
                )
            )
            record = self._repository.mark_published(
                record,
                provider_message_id=provider_message_id,
            )
        return record

    @staticmethod
    def _record(
        *,
        account_id: str,
        recipient: str,
        raw_message: bytes,
        content_hash: str,
        identity_hash: str,
        message_id_hash: str | None,
        sender: str | None,
        sender_address: str,
        subject: str | None,
        mime_part_count: int,
        attachment_count: int,
        content_types: tuple[str, ...],
    ) -> RawMailRecord:
        mail_id = sha256(f"{account_id}\0{identity_hash}".encode()).hexdigest()
        return RawMailRecord(
            id=mail_id,
            account_id=account_id,
            recipient=recipient,
            sender=sender,
            sender_address=sender_address,
            subject=subject,
            message_id_hash=message_id_hash,
            content_sha256=content_hash,
            size_bytes=len(raw_message),
            object_key=f"accounts/{account_id}/raw-mail/{mail_id}.eml",
            mime_part_count=mime_part_count,
            attachment_count=attachment_count,
            content_types=content_types,
            created_at=utc_now(),
        )
