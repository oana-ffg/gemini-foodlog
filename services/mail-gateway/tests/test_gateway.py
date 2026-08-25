from __future__ import annotations

import io
import os
import subprocess
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
import yaml
from google.cloud import firestore

from mail_gateway.adapters import FirestoreMailRepository
from mail_gateway.domain import MailIdentityCollision, RawMailRecord, UnknownRecipient
from mail_gateway.service import MailGatewayService
from main import MAX_RAW_MESSAGE_BYTES, MailGatewayApplication

DOMAIN = "gemini-foodlog-2026.appspotmail.com"
RECIPIENT = f"f-{'a' * 48}@{DOMAIN}"
ACCOUNT_ID = "account-a"
RAW_MESSAGE = (
    b"From: Nemlig test <orders@example.test>\r\n"
    b"To: forwarding-user@example.test\r\n"
    b"Message-ID: <order-123@example.test>\r\n"
    b"Subject: Test receipt\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Synthetic receipt body\r\n"
)


class FakeRepository:
    def __init__(self) -> None:
        self.records: dict[str, RawMailRecord] = {}

    def resolve_recipient(self, *, recipient: str, recipient_hash: str) -> str:
        if recipient != RECIPIENT or len(recipient_hash) != 64:
            raise UnknownRecipient
        return ACCOUNT_ID

    def reserve(self, record: RawMailRecord) -> RawMailRecord:
        existing = self.records.get(record.id)
        if existing is not None:
            if existing.content_sha256 != record.content_sha256:
                raise MailIdentityCollision
            return existing
        self.records[record.id] = record
        return record

    def mark_stored(self, record: RawMailRecord) -> RawMailRecord:
        stored = replace(record, status="stored")
        self.records[record.id] = stored
        return stored

    def mark_published(self, record: RawMailRecord, *, provider_message_id: str) -> RawMailRecord:
        published = replace(
            record,
            status="published",
            provider_message_id=provider_message_id,
            publish_attempt_count=record.publish_attempt_count + 1,
        )
        self.records[record.id] = published
        return published


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_if_absent(self, *, object_key: str, content: bytes) -> None:
        existing = self.objects.setdefault(object_key, content)
        if existing != content:
            raise ValueError("object collision")


class FakePublisher:
    def __init__(self) -> None:
        self.events = []
        self.failure: Exception | None = None

    def publish(self, event) -> str:
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure
        self.events.append(event)
        return f"message-{len(self.events)}"


@pytest.fixture
def gateway():
    repository = FakeRepository()
    object_store = FakeObjectStore()
    publisher = FakePublisher()
    service = MailGatewayService(
        domain=DOMAIN,
        repository=repository,
        object_store=object_store,
        event_publisher=publisher,
    )
    return service, repository, object_store, publisher


def test_receipt_is_private_durable_idempotent_and_publishes_only_references(
    gateway,
) -> None:
    service, repository, object_store, publisher = gateway

    first = service.receive(recipient=RECIPIENT, raw_message=RAW_MESSAGE)
    repeated = service.receive(recipient=RECIPIENT, raw_message=RAW_MESSAGE)

    assert first == repeated
    assert first.status == "published"
    assert first.account_id == ACCOUNT_ID
    assert first.recipient == RECIPIENT
    assert first.sender == "Nemlig test <orders@example.test>"
    assert first.subject == "Test receipt"
    assert first.message_id_hash is not None
    assert first.object_key == f"accounts/{ACCOUNT_ID}/raw-mail/{first.id}.eml"
    assert repository.records == {first.id: first}
    assert object_store.objects == {first.object_key: RAW_MESSAGE}
    assert len(publisher.events) == 1
    assert publisher.events[0].as_dict() == {
        "schema_version": 1,
        "kind": "raw_mail_stored",
        "account_id": ACCOUNT_ID,
        "mail_id": first.id,
    }
    assert RECIPIENT not in repr(publisher.events[0].as_dict())
    assert RAW_MESSAGE.decode() not in repr(publisher.events[0].as_dict())


def test_publish_failure_remains_retryable_without_duplicate_storage(gateway) -> None:
    service, repository, object_store, publisher = gateway
    publisher.failure = RuntimeError("temporary publication failure")

    with pytest.raises(RuntimeError, match="temporary publication"):
        service.receive(recipient=RECIPIENT, raw_message=RAW_MESSAGE)

    [stored] = repository.records.values()
    assert stored.status == "stored"
    assert len(object_store.objects) == 1

    recovered = service.receive(recipient=RECIPIENT, raw_message=RAW_MESSAGE)

    assert recovered.status == "published"
    assert len(object_store.objects) == 1
    assert len(publisher.events) == 1


def test_message_id_collision_preserves_different_raw_evidence(gateway) -> None:
    service, repository, object_store, publisher = gateway
    first = service.receive(recipient=RECIPIENT, raw_message=RAW_MESSAGE)
    changed = RAW_MESSAGE.replace(b"Synthetic receipt body", b"Changed forwarded body")

    second = service.receive(recipient=RECIPIENT, raw_message=changed)
    repeated_second = service.receive(recipient=RECIPIENT, raw_message=changed)

    assert first.id != second.id
    assert second == repeated_second
    assert len(repository.records) == len(object_store.objects) == len(publisher.events) == 2


def test_unknown_recipient_is_rejected_before_storage(gateway) -> None:
    service, repository, object_store, publisher = gateway

    with pytest.raises(UnknownRecipient):
        service.receive(
            recipient=f"f-{'b' * 48}@{DOMAIN}",
            raw_message=RAW_MESSAGE,
        )

    assert repository.records == {}
    assert object_store.objects == {}
    assert publisher.events == []


def wsgi_request(app, *, method: str, path: str, body: bytes) -> str:
    status = ""

    def start_response(value: str, _headers: list[tuple[str, str]]) -> None:
        nonlocal status
        status = value

    list(
        app(
            {
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body),
            },
            start_response,
        )
    )
    return status


def test_wsgi_boundary_limits_method_path_size_and_unknown_recipient(gateway) -> None:
    service, _, _, _ = gateway
    app = MailGatewayApplication(service)

    assert wsgi_request(app, method="GET", path=f"/_ah/mail/{RECIPIENT}", body=b"") == (
        "405 Method Not Allowed"
    )
    assert wsgi_request(app, method="POST", path="/not-mail", body=RAW_MESSAGE) == ("404 Not Found")
    assert (
        wsgi_request(
            app,
            method="POST",
            path=f"/_ah/mail/f-{'b' * 48}@{DOMAIN}",
            body=RAW_MESSAGE,
        )
        == "204 No Content"
    )
    assert (
        wsgi_request(
            app,
            method="POST",
            path=f"/_ah/mail/{RECIPIENT}",
            body=b"x" * (MAX_RAW_MESSAGE_BYTES + 1),
        )
        == "413 Payload Too Large"
    )


def test_app_engine_config_is_bounded_private_default_mail_service() -> None:
    config = yaml.safe_load(Path("app.yaml").read_text())

    assert "service" not in config
    assert config["runtime"] == "python313"
    assert config["instance_class"] == "F1"
    assert config["service_account"].startswith("foodlog-mail@")
    assert config["inbound_services"] == ["mail"]
    assert config["automatic_scaling"] == {
        "min_instances": 0,
        "max_instances": 1,
        "max_concurrent_requests": 8,
    }
    assert all(handler["login"] == "admin" for handler in config["handlers"])
    assert all(handler["auth_fail_action"] == "unauthorized" for handler in config["handlers"])
    assert config["env_variables"]["FOODLOG_MAIL_INBOUND_DOMAIN"] == DOMAIN


def test_app_engine_requirements_match_the_lockfile() -> None:
    result = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-hashes",
            "--no-header",
            "--no-emit-project",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == Path("requirements.txt").read_text()


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_repository_resolves_and_transitions_one_tenant_record() -> None:
    project_id = "gemini-foodlog-mail-gateway-test"
    database = firestore.Client(project=project_id)
    recipient_digest = sha256(RECIPIENT.encode()).hexdigest()
    database.collection("inbound_mail_routes").document(recipient_digest).set(
        {
            "account_id": ACCOUNT_ID,
            "address_id": "current",
            "status": "active",
        }
    )
    (
        database.collection("accounts")
        .document(ACCOUNT_ID)
        .collection("inbound_mail_addresses")
        .document("current")
        .set(
            {
                "account_id": ACCOUNT_ID,
                "address": RECIPIENT,
                "status": "active",
            }
        )
    )
    repository = FirestoreMailRepository(project_id=project_id)
    object_store = FakeObjectStore()
    publisher = FakePublisher()
    service = MailGatewayService(
        domain=DOMAIN,
        repository=repository,
        object_store=object_store,
        event_publisher=publisher,
    )

    record = service.receive(recipient=RECIPIENT, raw_message=RAW_MESSAGE)
    repeated = service.receive(recipient=RECIPIENT, raw_message=RAW_MESSAGE)
    snapshot = (
        database.collection("accounts")
        .document(ACCOUNT_ID)
        .collection("raw_mail")
        .document(record.id)
        .get()
    )

    assert repeated == record
    assert record.status == "published"
    assert snapshot.exists
    assert snapshot.get("status") == "published"
    assert snapshot.get("account_id") == ACCOUNT_ID
    assert snapshot.get("content_sha256") == record.content_sha256
    assert snapshot.get("provider_message_id") == "message-1"
    assert len(object_store.objects) == len(publisher.events) == 1
