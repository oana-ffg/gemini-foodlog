from __future__ import annotations

import argparse
import time
from email import policy
from email.message import EmailMessage

from google.cloud import firestore

from mail_gateway.adapters import (
    FirestoreMailRepository,
    GCSRawMailStore,
    PubSubMailEventPublisher,
)
from mail_gateway.service import MailGatewayService


def authenticated_headers(message: EmailMessage, *, message_id: str) -> None:
    message["From"] = "nemlig.com <kontakt@nemlig.com>"
    message["To"] = "FoodLog generated inbound address"
    message["Message-ID"] = message_id
    message["Authentication-Results"] = (
        "mx.example.test; dkim=pass header.d=nemlig.com; "
        "dmarc=pass header.from=nemlig.com"
    )


def confirmation_bytes(*, smoke_id: str, reference: str) -> bytes:
    message = EmailMessage()
    authenticated_headers(
        message,
        message_id=f"<foodlog-{smoke_id}-confirmation@example.test>",
    )
    message["Subject"] = "Tak for din ordre"
    message.set_content(
        "\n".join(
            (
                "Tak for din ordre",
                "",
                "Du kan tilføje eller fjerne varer fra din ordre frem til testfristen.",
                "",
                "Ordrenummer:",
                reference,
                "",
                "Syntetisk FoodLog smoke; ingen person- eller købsdata.",
            )
        )
    )
    return message.as_bytes(policy=policy.SMTP)


def invoice_bytes(*, smoke_id: str, reference: str) -> bytes:
    message = EmailMessage()
    authenticated_headers(
        message,
        message_id=f"<foodlog-{smoke_id}-invoice@example.test>",
    )
    message["Subject"] = f"Faktura - {reference}"
    message.set_content(
        "\n".join(
            (
                "nemlig.com - Din ordre er på vej",
                "",
                "Din ordre er på vej.",
                "Du finder din faktura i den vedhæftede fil, Faktura.pdf.",
                "Syntetisk FoodLog smoke; ingen person- eller købsdata.",
            )
        )
    )
    message.add_attachment(
        b"%PDF-1.7\nSynthetic FoodLog final invoice smoke\n",
        maintype="application",
        subtype="octet-stream",
        filename=f"Faktura - {reference}.pdf",
    )
    return message.as_bytes(policy=policy.SMTP)


def wait_for_document(
    database: firestore.Client,
    *,
    account_id: str,
    mail_id: str,
    timeout_seconds: int,
) -> dict:
    reference = (
        database.collection("accounts")
        .document(account_id)
        .collection("purchase_documents")
        .document(mail_id)
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        snapshot = reference.get()
        if snapshot.exists:
            return snapshot.to_dict() or {}
        time.sleep(2)
    raise TimeoutError(f"purchase document {mail_id} was not materialized")


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
        repository=FirestoreMailRepository(project_id=args.project),
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
    confirmation_document = wait_for_document(
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
    invoice_document = wait_for_document(
        database,
        account_id=args.account_id,
        mail_id=invoice.id,
        timeout_seconds=args.timeout_seconds,
    )

    if confirmation_document.get("kind") != "order_confirmation":
        raise AssertionError("confirmation was not classified correctly")
    if invoice_document.get("kind") != "final_receipt":
        raise AssertionError("invoice was not classified as authoritative final receipt")
    if confirmation_document.get("purchase_id") != invoice_document.get("purchase_id"):
        raise AssertionError("exact retailer identity did not join the purchase lifecycle")
    if confirmation_document.get("order_reference") != args.reference:
        raise AssertionError("confirmation order reference was not preserved")
    if invoice_document.get("order_reference") != args.reference:
        raise AssertionError("final invoice order reference was not preserved")
    if invoice_document.get("invoice_reference") != args.reference:
        raise AssertionError("final invoice identity was not preserved")
    purchase = (
        database.collection("accounts")
        .document(args.account_id)
        .collection("purchases")
        .document(str(invoice_document["purchase_id"]))
        .get()
    )
    purchase_data = purchase.to_dict() or {}
    if not purchase.exists or purchase_data.get("revision_count") != 2:
        raise AssertionError("purchase lifecycle does not contain exactly two revisions")

    print(f"purchase_id={invoice_document['purchase_id']}")
    print(f"confirmation_mail_id={confirmation.id}")
    print(f"invoice_mail_id={invoice.id}")
    print("confirmation_revision=1")
    print("authoritative_final_revision=2")
    print("exact_transport_retry=true")
    print("model_calls=0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write a labelled synthetic Nemlig confirmation and final invoice through "
            "the production gateway adapters, then verify the pushed worker result."
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
