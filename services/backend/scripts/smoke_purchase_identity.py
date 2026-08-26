from __future__ import annotations

import argparse
import asyncio

from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import PurchaseDocumentCandidate, PurchaseDocumentKind
from foodlog_backend.repository import purchase_identity_alias_id


async def smoke(args: argparse.Namespace) -> None:
    database = AsyncClient(project=args.project)
    account_ref = database.collection("accounts").document(args.account_id)

    async def raw_candidate(
        raw_mail_id: str,
        *,
        kind: PurchaseDocumentKind,
        invoice_reference: str | None = None,
    ) -> PurchaseDocumentCandidate:
        snapshot = await account_ref.collection("raw_mail").document(raw_mail_id).get()
        data = snapshot.to_dict() or {}
        content_sha256 = data.get("content_sha256")
        if (
            not snapshot.exists
            or data.get("account_id") != args.account_id
            or data.get("status") != "published"
            or not isinstance(content_sha256, str)
        ):
            raise RuntimeError("smoke raw mail is absent, foreign, or unpublished")
        return PurchaseDocumentCandidate(
            account_id=args.account_id,
            raw_mail_id=raw_mail_id,
            raw_content_sha256=content_sha256,
            kind=kind,
            order_reference=args.order_reference,
            invoice_reference=invoice_reference,
        )

    order = await raw_candidate(
        args.order_raw_mail_id,
        kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
    )
    receipt = await raw_candidate(
        args.receipt_raw_mail_id,
        kind=PurchaseDocumentKind.FINAL_RECEIPT,
        invoice_reference=args.invoice_reference,
    )
    repository = FirestoreRepository(
        project_id=args.project,
        public_account_limit=25,
        trial_image_limit=200,
        client=database,
    )

    first = await repository.attach_purchase_document(order)
    second = await repository.attach_purchase_document(receipt)
    retry = await repository.attach_purchase_document(receipt)
    if first.purchase.id != second.purchase.id or retry.document != second.document:
        raise AssertionError("exact business aliases did not connect idempotently")
    if [first.document.revision_number, second.document.revision_number] != [1, 2]:
        raise AssertionError("purchase document revisions are not ordered")
    if not retry.duplicate or retry.purchase.revision_count != 2:
        raise AssertionError("transport retry created another business revision")

    for kind, reference in (
        ("order", order.order_reference),
        ("invoice", receipt.invoice_reference),
    ):
        if reference is None:
            raise AssertionError("smoke references disappeared during normalization")
        alias_id = purchase_identity_alias_id(
            merchant="nemlig",
            kind=kind,
            reference=reference,
        )
        alias = await account_ref.collection("purchase_identities").document(alias_id).get()
        alias_data = alias.to_dict() or {}
        if not alias.exists or alias_data.get("purchase_id") != first.purchase.id:
            raise AssertionError("business alias does not point to the purchase")
        if reference in alias_data.values():
            raise AssertionError("business alias leaked its plaintext reference")

    query = (
        account_ref.collection("purchase_documents")
        .where(filter=FieldFilter("purchase_id", "==", first.purchase.id))
        .order_by("revision_number")
    )
    documents = [snapshot async for snapshot in query.stream()]
    if [snapshot.id for snapshot in documents] != [
        order.raw_mail_id,
        receipt.raw_mail_id,
    ]:
        raise AssertionError("ordered purchase-document query returned unexpected evidence")

    print(f"purchase_id={first.purchase.id}")
    print(f"order_document_id={first.document.id}")
    print(f"receipt_document_id={second.document.id}")
    print("revision_count=2")
    print("exact_retry_duplicate=true")
    print("plaintext_aliases=false")
    database.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect two retained synthetic raw messages through exact purchase aliases."
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--order-raw-mail-id", required=True)
    parser.add_argument("--receipt-raw-mail-id", required=True)
    parser.add_argument("--order-reference", required=True)
    parser.add_argument("--invoice-reference", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(smoke(parse_args()))
