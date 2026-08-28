from __future__ import annotations

import json
from dataclasses import asdict, replace
from hashlib import sha256

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import firestore, pubsub_v1, storage

from .domain import (
    MailIdentityCollision,
    MailQuotaPolicy,
    MailReservationNotFound,
    MailUsageBackfillRequired,
    RawMailRecord,
    RawMailStoredEventV1,
    RawMailUsage,
    UnknownRecipient,
    recipient_hash,
    utc_now,
    validate_raw_mail_object_key,
)


class FirestoreMailRepository:
    def __init__(
        self,
        *,
        project_id: str,
        domain: str,
        quota_policy: MailQuotaPolicy,
    ) -> None:
        self._client = firestore.Client(project=project_id)
        self._domain = domain
        self._quota_policy = quota_policy

    def admit_recipient(
        self,
        *,
        recipient: str,
        recipient_hash: str,
        size_bytes: int,
    ) -> str:
        transaction = self._client.transaction()

        @firestore.transactional
        def admit(transaction):
            account_id = self._active_account_for_recipient(
                transaction,
                recipient=recipient,
                recipient_hash=recipient_hash,
            )
            usage_ref = self._usage_reference(account_id)
            usage_snapshot = usage_ref.get(transaction=transaction)
            now = utc_now()
            usage = (
                self._usage_from_document(usage_snapshot.to_dict() or {})
                if usage_snapshot.exists
                else self._initial_usage_or_require_backfill(
                    transaction,
                    account_id=account_id,
                    now=now,
                )
            )
            admitted = usage.admit(
                self._quota_policy,
                size_bytes=size_bytes,
                now=now,
            )
            if usage_snapshot.exists:
                transaction.set(
                    usage_ref,
                    self._usage_document(admitted, account_id=account_id),
                )
            else:
                transaction.create(
                    usage_ref,
                    self._usage_document(admitted, account_id=account_id),
                )
            return account_id

        return admit(transaction)

    def reserve(self, record: RawMailRecord) -> tuple[RawMailRecord, bool]:
        reference = self._mail_reference(record)
        transaction = self._client.transaction()

        @firestore.transactional
        def reserve(transaction):
            account_id = self._active_account_for_recipient(
                transaction,
                recipient=record.recipient,
                recipient_hash=recipient_hash(
                    record.recipient,
                    expected_domain=self._domain,
                ),
            )
            if account_id != record.account_id:
                raise UnknownRecipient
            snapshot = reference.get(transaction=transaction)
            usage_ref = self._usage_reference(record.account_id)
            usage_snapshot = usage_ref.get(transaction=transaction)
            if snapshot.exists:
                existing = self._from_document(snapshot.to_dict() or {})
                self._validate_identity(existing, record)
                return existing, False
            if not usage_snapshot.exists:
                raise MailReservationNotFound
            usage = self._usage_from_document(usage_snapshot.to_dict() or {})
            reserved = usage.reserve(
                self._quota_policy,
                size_bytes=record.size_bytes,
                now=utc_now(),
            )
            transaction.create(reference, self._document(record))
            transaction.set(
                usage_ref,
                self._usage_document(reserved, account_id=record.account_id),
            )
            return record, True

        return reserve(transaction)

    def cancel(self, record: RawMailRecord) -> None:
        reference = self._mail_reference(record)
        usage_ref = self._usage_reference(record.account_id)
        transaction = self._client.transaction()

        @firestore.transactional
        def cancel(transaction):
            self._require_active_account(transaction, account_id=record.account_id)
            snapshot = reference.get(transaction=transaction)
            usage_snapshot = usage_ref.get(transaction=transaction)
            if not snapshot.exists:
                return
            current = self._from_document(snapshot.to_dict() or {})
            self._validate_identity(current, record)
            if current.status != "reserved" or not usage_snapshot.exists:
                raise MailReservationNotFound
            usage = self._usage_from_document(usage_snapshot.to_dict() or {})
            cancelled = usage.cancel(size_bytes=record.size_bytes, now=utc_now())
            transaction.delete(reference)
            transaction.set(
                usage_ref,
                self._usage_document(cancelled, account_id=record.account_id),
            )

        cancel(transaction)

    def mark_stored(self, record: RawMailRecord) -> RawMailRecord:
        reference = self._mail_reference(record)
        usage_ref = self._usage_reference(record.account_id)
        transaction = self._client.transaction()

        @firestore.transactional
        def store(transaction):
            self._require_active_account(transaction, account_id=record.account_id)
            snapshot = reference.get(transaction=transaction)
            usage_snapshot = usage_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise RuntimeError("reserved raw mail record disappeared")
            current = self._from_document(snapshot.to_dict() or {})
            self._validate_identity(current, record)
            if current.status in {"stored", "published"}:
                return current
            if current.status != "reserved" or not usage_snapshot.exists:
                raise MailReservationNotFound
            now = utc_now()
            usage = self._usage_from_document(usage_snapshot.to_dict() or {})
            stored_usage = usage.mark_stored(size_bytes=record.size_bytes, now=now)
            transaction.update(reference, {"status": "stored", "stored_at": now})
            transaction.set(
                usage_ref,
                self._usage_document(stored_usage, account_id=record.account_id),
            )
            return replace(current, status="stored", stored_at=now)

        return store(transaction)

    def mark_published(self, record: RawMailRecord, *, provider_message_id: str) -> RawMailRecord:
        reference = self._mail_reference(record)
        transaction = self._client.transaction()

        @firestore.transactional
        def publish(transaction):
            self._require_active_account(transaction, account_id=record.account_id)
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                raise RuntimeError("reserved raw mail record disappeared")
            current = self._from_document(snapshot.to_dict() or {})
            self._validate_identity(current, record)
            if current.status == "published":
                return current
            if current.status != "stored":
                raise RuntimeError("raw mail record has an invalid state transition")
            now = utc_now()
            updates = {
                "status": "published",
                "published_at": now,
                "provider_message_id": provider_message_id,
                "publish_attempt_count": current.publish_attempt_count + 1,
            }
            transaction.update(reference, updates)
            return replace(current, **updates)

        return publish(transaction)

    def _mail_reference(self, record: RawMailRecord):
        return (
            self._client.collection("accounts")
            .document(record.account_id)
            .collection("raw_mail")
            .document(record.id)
        )

    def _usage_reference(self, account_id: str):
        return (
            self._client.collection("accounts")
            .document(account_id)
            .collection("inbound_mail_usage")
            .document("current")
        )

    def _initial_usage_or_require_backfill(
        self,
        transaction,
        *,
        account_id: str,
        now,
    ) -> RawMailUsage:
        existing = list(
            self._client.collection("accounts")
            .document(account_id)
            .collection("raw_mail")
            .limit(1)
            .get(transaction=transaction)
        )
        if existing:
            raise MailUsageBackfillRequired("existing raw mail requires quota-ledger backfill")
        return RawMailUsage.create(self._quota_policy, now=now)

    def _require_active_account(self, transaction, *, account_id: str) -> None:
        account = (
            self._client.collection("accounts")
            .document(account_id)
            .get(transaction=transaction)
        )
        if not account.exists or account.get("status") != "active":
            raise UnknownRecipient

    def _active_account_for_recipient(
        self,
        transaction,
        *,
        recipient: str,
        recipient_hash: str,
    ) -> str:
        route = (
            self._client.collection("inbound_mail_routes")
            .document(recipient_hash)
            .get(transaction=transaction)
        )
        if not route.exists:
            raise UnknownRecipient
        route_data = route.to_dict() or {}
        account_id = route_data.get("account_id")
        if route_data.get("status") != "active" or not isinstance(account_id, str):
            raise UnknownRecipient
        account_ref = self._client.collection("accounts").document(account_id)
        address_ref = account_ref.collection("inbound_mail_addresses").document("current")
        account = account_ref.get(transaction=transaction)
        address = address_ref.get(transaction=transaction)
        address_data = address.to_dict() or {}
        route_generation = route_data.get("generation", 1)
        address_generation = address_data.get("generation", 1)
        if (
            not account.exists
            or account.get("status") != "active"
            or not address.exists
            or address_data.get("status") != "active"
            or address_data.get("account_id") != account_id
            or address_data.get("address") != recipient
            or not isinstance(route_generation, int)
            or isinstance(route_generation, bool)
            or route_generation < 1
            or route_generation != address_generation
        ):
            raise UnknownRecipient
        return account_id

    @staticmethod
    def _validate_identity(existing: RawMailRecord, candidate: RawMailRecord) -> None:
        if (
            existing.account_id != candidate.account_id
            or existing.content_sha256 != candidate.content_sha256
            or existing.object_key != candidate.object_key
        ):
            raise MailIdentityCollision

    @staticmethod
    def _document(record: RawMailRecord) -> dict:
        return {
            **asdict(record),
            "content_types": list(record.content_types),
            "schema_version": 1,
        }

    @staticmethod
    def _from_document(data: dict) -> RawMailRecord:
        data.pop("schema_version", None)
        data["content_types"] = tuple(data.get("content_types", ()))
        return RawMailRecord(**data)

    @staticmethod
    def _usage_document(usage: RawMailUsage, *, account_id: str) -> dict:
        return {"schema_version": 1, "account_id": account_id, **asdict(usage)}

    @staticmethod
    def _usage_from_document(data: dict) -> RawMailUsage:
        data.pop("schema_version", None)
        data.pop("account_id", None)
        return RawMailUsage(**data)


class GCSRawMailStore:
    def __init__(self, *, project_id: str, bucket_name: str) -> None:
        self._bucket = storage.Client(project=project_id).bucket(bucket_name)

    def put_if_absent(
        self,
        *,
        account_id: str,
        mail_id: str,
        object_key: str,
        content: bytes,
    ) -> None:
        validate_raw_mail_object_key(
            account_id=account_id,
            mail_id=mail_id,
            object_key=object_key,
        )
        blob = self._bucket.blob(object_key)
        try:
            blob.upload_from_string(
                content,
                content_type="message/rfc822",
                if_generation_match=0,
            )
        except PreconditionFailed:
            if not self.contains_exact(
                account_id=account_id,
                mail_id=mail_id,
                object_key=object_key,
                content=content,
            ):
                raise RuntimeError("raw-mail object disappeared after create conflict") from None
            return

    def contains_exact(
        self,
        *,
        account_id: str,
        mail_id: str,
        object_key: str,
        content: bytes,
    ) -> bool:
        validate_raw_mail_object_key(
            account_id=account_id,
            mail_id=mail_id,
            object_key=object_key,
        )
        try:
            existing = self._bucket.blob(object_key).download_as_bytes()
        except NotFound:
            return False
        if sha256(existing).digest() != sha256(content).digest():
            raise MailIdentityCollision("raw-mail object content collision")
        return True


class PubSubMailEventPublisher:
    def __init__(self, *, topic: str) -> None:
        self._topic = topic
        self._publisher = pubsub_v1.PublisherClient()

    def publish(self, event: RawMailStoredEventV1) -> str:
        return self._publisher.publish(
            self._topic,
            json.dumps(event.as_dict(), separators=(",", ":")).encode(),
            event_kind=event.kind,
            schema_version=str(event.schema_version),
        ).result(timeout=30)
