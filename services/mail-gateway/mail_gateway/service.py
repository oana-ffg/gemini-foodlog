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
    def admit_recipient(
        self,
        *,
        recipient: str,
        recipient_hash: str,
        size_bytes: int,
    ) -> str: ...

    def reserve(self, record: RawMailRecord) -> tuple[RawMailRecord, bool]: ...

    def cancel(self, record: RawMailRecord) -> None: ...

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

    def contains_exact(
        self,
        *,
        account_id: str,
        mail_id: str,
        object_key: str,
        content: bytes,
    ) -> bool: ...


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
        normalized_recipient, account_id = self._admit_attempt(
            recipient=recipient,
            size_bytes=len(raw_message),
        )

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
        return self._persist_and_publish(record, raw_message=raw_message)

    def record_attempt(self, *, recipient: str, size_bytes: int) -> None:
        """Charge a known recipient for input discarded before MIME parsing."""

        self._admit_attempt(recipient=recipient, size_bytes=size_bytes)

    def _admit_attempt(self, *, recipient: str, size_bytes: int) -> tuple[str, str]:
        normalized_recipient = normalize_recipient(recipient, expected_domain=self._domain)
        account_id = self._repository.admit_recipient(
            recipient=normalized_recipient,
            recipient_hash=recipient_hash(
                normalized_recipient,
                expected_domain=self._domain,
            ),
            size_bytes=size_bytes,
        )
        if not account_id:
            raise UnknownRecipient
        return normalized_recipient, account_id

    def _persist_and_publish(
        self,
        record: RawMailRecord,
        *,
        raw_message: bytes,
    ) -> RawMailRecord:
        content_hash = record.content_sha256
        identity_hash = record.message_id_hash or content_hash
        try:
            record, created = self._repository.reserve(record)
        except MailIdentityCollision:
            collision_safe_hash = sha256(f"{identity_hash}\0{content_hash}".encode()).hexdigest()
            record, created = self._repository.reserve(
                self._record(
                    account_id=record.account_id,
                    recipient=record.recipient,
                    raw_message=raw_message,
                    content_hash=content_hash,
                    identity_hash=collision_safe_hash,
                    message_id_hash=record.message_id_hash,
                    sender=record.sender,
                    sender_address=record.sender_address,
                    subject=record.subject,
                    mime_part_count=record.mime_part_count,
                    attachment_count=record.attachment_count,
                    content_types=record.content_types,
                )
            )

        if record.status == "published":
            return record
        object_persisted = False
        try:
            self._object_store.put_if_absent(
                account_id=record.account_id,
                mail_id=record.id,
                object_key=record.object_key,
                content=raw_message,
            )
            # A clean return means the immutable object now exists, whether this
            # request created it or an earlier interrupted attempt did.
            object_persisted = True
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
        except Exception as error:
            cleanup_errors: list[Exception] = []
            if created and not object_persisted:
                try:
                    object_persisted = self._object_store.contains_exact(
                        account_id=record.account_id,
                        mail_id=record.id,
                        object_key=record.object_key,
                        content=raw_message,
                    )
                except Exception as probe_error:
                    # An unverifiable upload is accounted as persisted. Releasing
                    # capacity here could leave untracked immutable bytes in GCS.
                    object_persisted = True
                    cleanup_errors.append(probe_error)
                if not object_persisted:
                    try:
                        self._repository.cancel(record)
                    except Exception as cleanup_error:
                        cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise ExceptionGroup(
                    "Raw-mail acceptance and cleanup both failed",
                    [error, *cleanup_errors],
                ) from error
            raise
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
