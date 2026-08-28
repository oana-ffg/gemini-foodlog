from __future__ import annotations

import argparse
import time
from pathlib import Path

from google.cloud import firestore

from mail_gateway.adapters import (
    FirestoreMailRepository,
    GCSRawMailStore,
    PubSubMailEventPublisher,
)
from mail_gateway.config import quota_policy_from_environment
from mail_gateway.service import MailGatewayService

FIXTURE_ROOT = Path(__file__).parents[2] / "backend" / "tests" / "fixtures" / "nemlig"


def confirmation_bytes(*, smoke_id: str, reference: str) -> bytes:
    return (
        (FIXTURE_ROOT / "order-confirmation.eml")
        .read_bytes()
        .replace(b"9000000001", reference.encode())
        .replace(
            b"<synthetic-order-confirmation@example.test>",
            f"<foodlog-{smoke_id}-confirmation@example.test>".encode(),
        )
    )


def invoice_bytes(*, smoke_id: str, reference: str) -> bytes:
    return (
        (FIXTURE_ROOT / "final-invoice.eml")
        .read_bytes()
        .replace(b"9000000001", reference.encode())
        .replace(
            b"<synthetic-final-invoice@example.test>",
            f"<foodlog-{smoke_id}-invoice@example.test>".encode(),
        )
    )


def wait_for_authentication(
    database: firestore.Client,
    *,
    account_id: str,
    mail_id: str,
    timeout_seconds: int,
) -> dict:
    reference = (
        database.collection("accounts")
        .document(account_id)
        .collection("raw_mail_authentication")
        .document(mail_id)
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        snapshot = reference.get()
        if snapshot.exists:
            return snapshot.to_dict() or {}
        time.sleep(2)
    raise TimeoutError(f"raw-mail authentication {mail_id} was not materialized")


def assert_no_purchase_artifacts(
    database: firestore.Client,
    *,
    account_id: str,
    mail_id: str,
) -> None:
    account = database.collection("accounts").document(account_id)
    for collection in ("purchase_documents", "purchase_normalizations"):
        if account.collection(collection).document(mail_id).get().exists:
            raise AssertionError(f"forged mail created {collection}/{mail_id}")


def smoke(args: argparse.Namespace) -> None:
    if not args.reference.isdigit() or len(args.reference) != 10:
        raise ValueError("--reference must be exactly 10 digits")
    database = firestore.Client(project=args.project)
    address_snapshot = (
        database.collection("accounts")
        .document(args.account_id)
        .collection("inbound_mail_addresses")
        .document("current")
        .get()
    )
    address_data = address_snapshot.to_dict() or {}
    recipient = address_data.get("address")
    if (
        not address_snapshot.exists
        or address_data.get("status") != "active"
        or not isinstance(recipient, str)
    ):
        raise RuntimeError("test account has no active generated inbound address")

    gateway = MailGatewayService(
        domain=args.domain,
        repository=FirestoreMailRepository(
            project_id=args.project,
            domain=args.domain,
            quota_policy=quota_policy_from_environment(),
        ),
        object_store=GCSRawMailStore(project_id=args.project, bucket_name=args.bucket),
        event_publisher=PubSubMailEventPublisher(topic=args.topic),
    )
    confirmation_message = confirmation_bytes(
        smoke_id=args.smoke_id,
        reference=args.reference,
    )
    confirmation = gateway.receive(
        recipient=recipient,
        raw_message=confirmation_message,
    )
    confirmation_authentication = wait_for_authentication(
        database,
        account_id=args.account_id,
        mail_id=confirmation.id,
        timeout_seconds=args.timeout_seconds,
    )
    invoice_message = invoice_bytes(smoke_id=args.smoke_id, reference=args.reference)
    invoice = gateway.receive(
        recipient=recipient,
        raw_message=invoice_message,
    )
    repeated_invoice = gateway.receive(
        recipient=recipient,
        raw_message=invoice_message,
    )
    if repeated_invoice != invoice:
        raise AssertionError("final invoice transport retry was not idempotent")
    invoice_authentication = wait_for_authentication(
        database,
        account_id=args.account_id,
        mail_id=invoice.id,
        timeout_seconds=args.timeout_seconds,
    )
    if confirmation_authentication.get("outcome") != "untrusted":
        raise AssertionError("forged confirmation was not marked untrusted")
    if invoice_authentication.get("outcome") != "untrusted":
        raise AssertionError("forged invoice was not marked untrusted")
    assert_no_purchase_artifacts(
        database,
        account_id=args.account_id,
        mail_id=confirmation.id,
    )
    assert_no_purchase_artifacts(
        database,
        account_id=args.account_id,
        mail_id=invoice.id,
    )

    print(f"confirmation_mail_id={confirmation.id}")
    print(f"invoice_mail_id={invoice.id}")
    print("exact_transport_retry=true")
    print("forged_authentication_headers_rejected=true")
    print("purchase_documents_created=0")
    print("model_calls=0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write labelled Nemlig-shaped fixtures with forged authentication headers "
            "through production adapters, then verify the worker rejects both."
        )
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--smoke-id", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
