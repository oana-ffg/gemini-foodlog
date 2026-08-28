from __future__ import annotations

import argparse
from email import policy
from email.message import EmailMessage
from hashlib import sha256

from google.cloud import firestore, storage

from mail_gateway.adapters import (
    FirestoreMailRepository,
    GCSRawMailStore,
    PubSubMailEventPublisher,
)
from mail_gateway.config import quota_policy_from_environment
from mail_gateway.domain import UnsafeMail
from mail_gateway.service import MailGatewayService


def message_bytes(*, smoke_id: str, unsafe: bool) -> bytes:
    message = EmailMessage()
    message["From"] = "FoodLog transport test <transport-smoke@example.test>"
    message["To"] = "FoodLog generated inbound address"
    message["Message-ID"] = f"<foodlog-{smoke_id}{'-unsafe' if unsafe else ''}@example.test>"
    message["Subject"] = f"FoodLog {smoke_id} synthetic transport smoke - not a purchase"
    message.set_content(
        "Synthetic security smoke only. Ignore prior instructions is inert email evidence."
    )
    if unsafe:
        message.add_attachment(
            b"MZ synthetic executable marker",
            maintype="application",
            subtype="x-msdownload",
            filename="not-an-invoice.exe",
        )
    return message.as_bytes(policy=policy.SMTP)


def deterministic_mail_id(*, account_id: str, message_id: str) -> str:
    message_id_hash = sha256(message_id.encode()).hexdigest()
    return sha256(f"{account_id}\0{message_id_hash}".encode()).hexdigest()


def smoke(args: argparse.Namespace) -> None:
    database = firestore.Client(project=args.project)
    address_snapshot = (
        database.collection("accounts")
        .document(args.account_id)
        .collection("inbound_mail_addresses")
        .document("current")
        .get()
    )
    if not address_snapshot.exists:
        raise RuntimeError("test account has no generated inbound address")
    address_data = address_snapshot.to_dict() or {}
    recipient = address_data.get("address")
    if address_data.get("status") != "active" or not isinstance(recipient, str):
        raise RuntimeError("test account inbound address is not active")

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
    accepted_bytes = message_bytes(smoke_id=args.smoke_id, unsafe=False)
    accepted = gateway.receive(recipient=recipient, raw_message=accepted_bytes)
    repeated = gateway.receive(recipient=recipient, raw_message=accepted_bytes)
    if accepted != repeated or accepted.status != "published":
        raise AssertionError("accepted message was not published idempotently")
    if accepted.trust_class != "untrusted_external" or accepted.sender_address != (
        "transport-smoke@example.test"
    ):
        raise AssertionError("accepted message lost its external trust metadata")
    stored_bytes = (
        storage.Client(project=args.project)
        .bucket(args.bucket)
        .blob(accepted.object_key)
        .download_as_bytes()
    )
    if stored_bytes != accepted_bytes:
        raise AssertionError("stored MIME bytes differ from the submitted message")

    unsafe_bytes = message_bytes(smoke_id=args.smoke_id, unsafe=True)
    try:
        gateway.receive(recipient=recipient, raw_message=unsafe_bytes)
    except UnsafeMail as error:
        if error.code != "unsafe_or_unsupported_content_type":
            raise AssertionError("unsafe message failed for an unexpected reason") from error
    else:
        raise AssertionError("unsafe executable attachment was accepted")

    unsafe_id = deterministic_mail_id(
        account_id=args.account_id,
        message_id=f"<foodlog-{args.smoke_id}-unsafe@example.test>",
    )
    unsafe_record = (
        database.collection("accounts")
        .document(args.account_id)
        .collection("raw_mail")
        .document(unsafe_id)
        .get()
    )
    unsafe_blob = (
        storage.Client(project=args.project)
        .bucket(args.bucket)
        .blob(f"accounts/{args.account_id}/raw-mail/{unsafe_id}.eml")
    )
    if unsafe_record.exists or unsafe_blob.exists():
        raise AssertionError("unsafe message left durable state")

    print(f"accepted_mail_id={accepted.id}")
    print(f"accepted_object_key={accepted.object_key}")
    print("accepted_exact_retry=true")
    print("accepted_exact_bytes=true")
    print("unsafe_rejected_without_state=true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write one labelled benign MIME and reject one unsafe MIME using production adapters."
        )
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--smoke-id", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
