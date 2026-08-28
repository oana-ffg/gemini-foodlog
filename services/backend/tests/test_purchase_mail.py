import asyncio
import json
from base64 import b64encode
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from foodlog_backend.mail_events import RawMailStoredEventV1
from foodlog_backend.mail_worker_app import MailWorkerSettings, create_mail_worker_app
from foodlog_backend.models import (
    PurchaseDocumentKind,
    PurchaseReconciliationDisposition,
    RawMailAuthentication,
    RawMailAuthenticationOutcome,
    RawMailProcessingOutcome,
)
from foodlog_backend.purchase_mail import (
    MailClassificationOutcome,
    classify_nemlig_purchase_email,
    raw_mail_object_key,
)
from foodlog_backend.purchase_pdf_limits import PARSER_VERSION, PurchasePdfIsolationFailure
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.storage import InMemoryObjectStore

FIXTURES = Path(__file__).parent / "fixtures" / "nemlig"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def trusted_authentication(
    raw_message: bytes,
    *,
    account_id: str,
    mail_id: str,
) -> RawMailAuthentication:
    return RawMailAuthentication(
        id=mail_id,
        account_id=account_id,
        raw_mail_id=mail_id,
        raw_content_sha256=sha256(raw_message).hexdigest(),
        outcome=RawMailAuthenticationOutcome.ALIGNED_DKIM_PASS,
        signer_domain="nemlig.com",
        signed_headers=("from", "subject", "mime-version", "content-type"),
        verifier_version="test-aligned-dkim-v1",
    )


class FixtureMailAuthenticator:
    async def authenticate(
        self,
        raw_message: bytes,
        *,
        account_id: str,
        raw_mail_id: str,
    ) -> RawMailAuthentication:
        return trusted_authentication(
            raw_message,
            account_id=account_id,
            mail_id=raw_mail_id,
        )


def classify(name: str, *, account_id: str = "account-a"):
    raw_message = fixture(name)
    mail_id = sha256(raw_message).hexdigest()
    return classify_nemlig_purchase_email(
        raw_message,
        account_id=account_id,
        mail_id=mail_id,
        authentication=trusted_authentication(
            raw_message,
            account_id=account_id,
            mail_id=mail_id,
        ),
    )


def push_envelope(event: RawMailStoredEventV1, *, message_id: str) -> dict:
    payload = json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "message": {
            "data": b64encode(payload).decode(),
            "messageId": message_id,
        },
        "subscription": "projects/test/subscriptions/foodlog-mail-consumer",
    }


def worker_settings() -> MailWorkerSettings:
    return MailWorkerSettings(
        environment="test",
        gcp_project_id="test-project",
        raw_mail_bucket="test-raw-mail",
    )


async def prepare_worker_message(owner_user_id: str, content: bytes):
    repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    store = InMemoryObjectStore()
    account = await repository.provision_account(owner_user_id)
    mail_id = sha256(content).hexdigest()
    key = raw_mail_object_key(account_id=account.id, mail_id=mail_id)
    await store.put(account.id, key, content, "message/rfc822")
    await repository.seed_published_raw_mail(
        account_id=account.id,
        raw_mail_id=mail_id,
        content_sha256=sha256(content).hexdigest(),
    )
    return repository, store, account.id, RawMailStoredEventV1(
        account_id=account.id,
        mail_id=mail_id,
    )


def test_real_redacted_structures_classify_confirmation_and_final_invoice() -> None:
    confirmation = classify("order-confirmation.eml")
    invoice = classify("final-invoice.eml")

    assert confirmation.outcome == MailClassificationOutcome.PURCHASE_DOCUMENT
    assert confirmation.candidate is not None
    assert confirmation.candidate.kind == PurchaseDocumentKind.ORDER_CONFIRMATION
    assert confirmation.candidate.order_reference == "9000000001"
    assert confirmation.candidate.invoice_reference is None
    assert invoice.outcome == MailClassificationOutcome.PURCHASE_DOCUMENT
    assert invoice.candidate is not None
    assert invoice.candidate.kind == PurchaseDocumentKind.FINAL_RECEIPT
    assert invoice.candidate.order_reference == "9000000001"
    assert invoice.candidate.invoice_reference == "9000000001"
    assert invoice.candidate.raw_content_sha256 == sha256(fixture("final-invoice.eml")).hexdigest()


def test_unrelated_nemlig_mail_and_unverified_sender_are_not_purchase_evidence() -> None:
    unrelated = classify("delivery-update.eml")
    spoof = fixture("order-confirmation.eml").replace(
        b"Authentication-Results: mx.example.test; dkim=pass header.d=nemlig.com; "
        b"dmarc=pass header.from=nemlig.com\n",
        b"",
    )
    spoof_result = classify_nemlig_purchase_email(
        spoof,
        account_id="account-a",
        mail_id=sha256(spoof).hexdigest(),
    )

    assert unrelated.outcome == MailClassificationOutcome.UNSUPPORTED_NEMLIG
    assert unrelated.candidate is None
    assert spoof_result.outcome == MailClassificationOutcome.NOT_TRUSTED_NEMLIG
    assert spoof_result.candidate is None


@pytest.mark.parametrize(
    "forged_header",
    [b"Authentication-Results", b"ARC-Authentication-Results"],
)
def test_sender_controlled_authentication_results_cannot_authenticate_purchase(
    forged_header: bytes,
) -> None:
    forged = fixture("order-confirmation.eml").replace(
        b"Authentication-Results",
        forged_header,
    )

    result = classify_nemlig_purchase_email(
        forged,
        account_id="account-a",
        mail_id=sha256(forged).hexdigest(),
    )

    assert result.outcome == MailClassificationOutcome.NOT_TRUSTED_NEMLIG
    assert result.candidate is None


def test_mail_worker_persists_untrusted_verdict_for_forged_headers() -> None:
    async def prepare():
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        store = InMemoryObjectStore()
        account = await repository.provision_account("forged-mail-worker-owner")
        content = fixture("order-confirmation.eml")
        mail_id = sha256(content).hexdigest()
        key = raw_mail_object_key(account_id=account.id, mail_id=mail_id)
        await store.put(account.id, key, content, "message/rfc822")
        await repository.seed_published_raw_mail(
            account_id=account.id,
            raw_mail_id=mail_id,
            content_sha256=sha256(content).hexdigest(),
        )
        return repository, store, account.id, RawMailStoredEventV1(
            account_id=account.id,
            mail_id=mail_id,
        )

    repository, store, account_id, event = asyncio.run(prepare())
    app = create_mail_worker_app(
        worker_settings(),
        repository=repository,
        object_store=store,
    )

    with TestClient(app) as client:
        first = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(event, message_id="forged-mail-message-1"),
        )
        retry = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(event, message_id="forged-mail-message-2"),
        )

    authentication = asyncio.run(
        repository.raw_mail_authentication(
            account_id=account_id,
            raw_mail_id=event.mail_id,
        )
    )
    assert first.status_code == retry.status_code == 204
    assert authentication is not None
    assert authentication.outcome == RawMailAuthenticationOutcome.UNTRUSTED
    assert repository._purchase_documents == {}


def test_invoice_requires_matching_pdf_filename_and_magic_bytes() -> None:
    wrong_name = fixture("final-invoice.eml").replace(
        b"Faktura - 9000000001.pdf",
        b"Faktura - 9000000002.pdf",
    )
    wrong_magic = fixture("final-invoice.eml").replace(
        b"JVBERi0xLjMK",
        b"Tk9ULUEtUERG",
    )

    for raw_message in (wrong_name, wrong_magic):
        result = classify_nemlig_purchase_email(
            raw_message,
            account_id="account-a",
            mail_id=sha256(raw_message).hexdigest(),
            authentication=trusted_authentication(
                raw_message,
                account_id="account-a",
                mail_id=sha256(raw_message).hexdigest(),
            ),
        )
        assert result.outcome == MailClassificationOutcome.UNSUPPORTED_NEMLIG
        assert result.candidate is None


def test_mail_worker_preserves_confirmation_then_final_invoice_as_one_purchase() -> None:
    async def prepare():
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        store = InMemoryObjectStore()
        account = await repository.provision_account("mail-worker-owner")
        events = []
        for fixture_name in ("order-confirmation.eml", "final-invoice.eml"):
            content = fixture(fixture_name)
            mail_id = sha256(content).hexdigest()
            key = raw_mail_object_key(account_id=account.id, mail_id=mail_id)
            await store.put(account.id, key, content, "message/rfc822")
            await repository.seed_published_raw_mail(
                account_id=account.id,
                raw_mail_id=mail_id,
                content_sha256=sha256(content).hexdigest(),
            )
            events.append(RawMailStoredEventV1(account_id=account.id, mail_id=mail_id))
        return repository, store, account.id, events

    repository, store, account_id, events = asyncio.run(prepare())
    app = create_mail_worker_app(
        worker_settings(),
        repository=repository,
        object_store=store,
        mail_authenticator=FixtureMailAuthenticator(),
    )

    with TestClient(app) as client:
        health = client.get("/health")
        confirmation = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(events[0], message_id="mail-message-1"),
        )
        invoice = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(events[1], message_id="mail-message-2"),
        )
        duplicate = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(events[1], message_id="mail-message-3"),
        )

    documents = [
        document
        for (scope, _), document in repository._purchase_documents.items()
        if scope == account_id
    ]
    purchases = [
        purchase for (scope, _), purchase in repository._purchases.items() if scope == account_id
    ]
    normalizations = [
        normalization
        for (scope, _), normalization in repository._purchase_normalizations.items()
        if scope == account_id
    ]
    items = [item for (scope, _), item in repository._purchase_items.items() if scope == account_id]
    charges = [
        charge for (scope, _), charge in repository._purchase_charges.items() if scope == account_id
    ]
    reconciliation = repository._purchase_reconciliations[(account_id, purchases[0].id)]
    assert health.json() == {"status": "ok", "mode": "test"}
    assert confirmation.status_code == invoice.status_code == duplicate.status_code == 204
    assert len(purchases) == 1
    assert purchases[0].revision_count == 2
    assert [document.kind for document in documents] == [
        PurchaseDocumentKind.ORDER_CONFIRMATION,
        PurchaseDocumentKind.FINAL_RECEIPT,
    ]
    assert [document.revision_number for document in documents] == [1, 2]
    assert len(normalizations) == 2
    assert {normalization.parser_version for normalization in normalizations} == {
        PARSER_VERSION
    }
    assert len(items) == 4
    assert len(charges) == 11
    assert reconciliation.unresolved_item_count == 0
    assert {item.disposition for item in reconciliation.items} == {
        PurchaseReconciliationDisposition.DELIVERED_AS_ORDERED
    }


def test_mail_worker_durably_acknowledges_terminal_pdf_rejection() -> None:
    async def prepare():
        message = BytesParser(policy=policy.default).parsebytes(fixture("final-invoice.eml"))
        for part in message.walk():
            if part.get_filename():
                part.set_payload(b64encode(b"%PDF-invalid-structure").decode())
                break
        content = message.as_bytes()
        return await prepare_worker_message(
            "rejected-pdf-owner",
            content,
        )

    repository, store, account_id, event = asyncio.run(prepare())
    app = create_mail_worker_app(
        worker_settings(),
        repository=repository,
        object_store=store,
        mail_authenticator=FixtureMailAuthenticator(),
    )

    with TestClient(app) as client:
        first = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(event, message_id="rejected-pdf-message-1"),
        )
        retry = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(event, message_id="rejected-pdf-message-2"),
        )

    disposition = asyncio.run(
        repository.raw_mail_processing_disposition(
            account_id=account_id,
            raw_mail_id=event.mail_id,
        )
    )
    assert first.status_code == retry.status_code == 204
    assert disposition is not None
    assert disposition.outcome == RawMailProcessingOutcome.TERMINAL_REJECTED
    assert disposition.failure_code == "pdf_structure_rejected"
    assert repository._purchase_documents == {}
    assert repository._purchase_normalizations == {}


def test_mail_worker_retries_operational_pdf_isolation_failure(monkeypatch) -> None:
    repository, store, account_id, event = asyncio.run(
        prepare_worker_message("pdf-isolation-failure-owner", fixture("final-invoice.eml"))
    )

    def fail_isolation(*args, **kwargs):
        del args, kwargs
        raise PurchasePdfIsolationFailure("pdf_subprocess_unavailable")

    monkeypatch.setattr(
        "foodlog_backend.mail_worker_app.parse_purchase_document",
        fail_isolation,
    )
    app = create_mail_worker_app(
        worker_settings(),
        repository=repository,
        object_store=store,
        mail_authenticator=FixtureMailAuthenticator(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(event, message_id="pdf-isolation-failure-message"),
        )

    disposition = asyncio.run(
        repository.raw_mail_processing_disposition(
            account_id=account_id,
            raw_mail_id=event.mail_id,
        )
    )
    assert response.status_code == 503
    assert disposition is None
    assert repository._purchase_documents == {}


def test_mail_worker_rejects_bad_event_and_retries_missing_object() -> None:
    repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    app = create_mail_worker_app(
        worker_settings(),
        repository=repository,
        object_store=InMemoryObjectStore(),
    )
    missing = RawMailStoredEventV1(account_id="account-a", mail_id="a" * 64)

    with TestClient(app) as client:
        malformed = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(
                missing.model_copy(update={"kind": "wrong"}),
                message_id="bad-mail-message",
            ),
        )
        failed = client.post(
            "/internal/pubsub/raw-mail-stored",
            json=push_envelope(missing, message_id="missing-mail-message"),
        )

    assert malformed.status_code == 400
    assert malformed.json() == {"detail": "invalid_pubsub_event"}
    assert failed.status_code == 503
    assert failed.json() == {"detail": "mail_classification_failed"}
