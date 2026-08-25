from __future__ import annotations

import json
from dataclasses import asdict, replace

from google.api_core.exceptions import PreconditionFailed
from google.cloud import firestore, pubsub_v1, storage

from .domain import (
    MailIdentityCollision,
    RawMailRecord,
    RawMailStoredEventV1,
    UnknownRecipient,
    utc_now,
)


class FirestoreMailRepository:
    def __init__(self, *, project_id: str) -> None:
        self._client = firestore.Client(project=project_id)

    def resolve_recipient(self, *, recipient: str, recipient_hash: str) -> str:
        route = self._client.collection("inbound_mail_routes").document(recipient_hash).get()
        if not route.exists:
            raise UnknownRecipient
        route_data = route.to_dict() or {}
        account_id = route_data.get("account_id")
        if route_data.get("status") != "active" or not isinstance(account_id, str):
            raise UnknownRecipient
        address = (
            self._client.collection("accounts")
            .document(account_id)
            .collection("inbound_mail_addresses")
            .document("current")
            .get()
        )
        address_data = address.to_dict() or {}
        if (
            not address.exists
            or address_data.get("status") != "active"
            or address_data.get("account_id") != account_id
            or address_data.get("address") != recipient
        ):
            raise UnknownRecipient
        return account_id

    def reserve(self, record: RawMailRecord) -> RawMailRecord:
        reference = self._mail_reference(record)
        transaction = self._client.transaction()

        @firestore.transactional
        def reserve(transaction):
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists:
                existing = self._from_document(snapshot.to_dict() or {})
                self._validate_identity(existing, record)
                return existing
            transaction.create(reference, self._document(record))
            return record

        return reserve(transaction)

    def mark_stored(self, record: RawMailRecord) -> RawMailRecord:
        return self._transition(record, expected="reserved", target="stored")

    def mark_published(self, record: RawMailRecord, *, provider_message_id: str) -> RawMailRecord:
        return self._transition(
            record,
            expected="stored",
            target="published",
            provider_message_id=provider_message_id,
        )

    def _transition(
        self,
        record: RawMailRecord,
        *,
        expected: str,
        target: str,
        provider_message_id: str | None = None,
    ) -> RawMailRecord:
        reference = self._mail_reference(record)
        transaction = self._client.transaction()

        @firestore.transactional
        def transition(transaction):
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                raise RuntimeError("reserved raw mail record disappeared")
            current = self._from_document(snapshot.to_dict() or {})
            self._validate_identity(current, record)
            if current.status == target or current.status == "published":
                return current
            if current.status != expected:
                raise RuntimeError("raw mail record has an invalid state transition")
            now = utc_now()
            updates = {
                "status": target,
                "publish_attempt_count": current.publish_attempt_count,
            }
            if target == "stored":
                updates["stored_at"] = now
            else:
                updates.update(
                    {
                        "published_at": now,
                        "provider_message_id": provider_message_id,
                        "publish_attempt_count": current.publish_attempt_count + 1,
                    }
                )
            transaction.update(reference, updates)
            return replace(current, **updates)

        return transition(transaction)

    def _mail_reference(self, record: RawMailRecord):
        return (
            self._client.collection("accounts")
            .document(record.account_id)
            .collection("raw_mail")
            .document(record.id)
        )

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
        return {**asdict(record), "schema_version": 1}

    @staticmethod
    def _from_document(data: dict) -> RawMailRecord:
        data.pop("schema_version", None)
        return RawMailRecord(**data)


class GCSRawMailStore:
    def __init__(self, *, project_id: str, bucket_name: str) -> None:
        self._bucket = storage.Client(project=project_id).bucket(bucket_name)

    def put_if_absent(self, *, object_key: str, content: bytes) -> None:
        try:
            self._bucket.blob(object_key).upload_from_string(
                content,
                content_type="message/rfc822",
                if_generation_match=0,
            )
        except PreconditionFailed:
            return


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
