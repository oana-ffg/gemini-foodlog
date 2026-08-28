from __future__ import annotations

import asyncio
import os
from hashlib import sha256

import pytest
from google.cloud.firestore_v1.async_client import AsyncClient
from pydantic import ValidationError

from foodlog_backend.errors import (
    PurchaseDocumentConflict,
    PurchaseIdentityConflict,
    RawMailAuthenticationConflict,
    RawMailNotFound,
)
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    ParsedPurchaseDocument,
    PurchaseChargeDraft,
    PurchaseChargeKind,
    PurchaseDocumentCandidate,
    PurchaseDocumentKind,
    PurchaseItemDisposition,
    PurchaseItemDraft,
    PurchaseReconciliationDisposition,
    RawMailAuthentication,
    RawMailAuthenticationOutcome,
)
from foodlog_backend.repository import InMemoryRepository, purchase_identity_alias_id

from .purchase_test_support import (
    seed_authenticated_raw_mail,
    trusted_authentication_for_candidate,
)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def candidate(
    account_id: str,
    label: str,
    *,
    kind: PurchaseDocumentKind = PurchaseDocumentKind.UNKNOWN,
    order_reference: str | None = None,
    invoice_reference: str | None = None,
) -> PurchaseDocumentCandidate:
    return PurchaseDocumentCandidate(
        account_id=account_id,
        raw_mail_id=digest(f"raw:{label}"),
        raw_content_sha256=digest(f"content:{label}"),
        kind=kind,
        order_reference=order_reference,
        invoice_reference=invoice_reference,
    )


async def seed(repository, *candidates: PurchaseDocumentCandidate) -> None:
    for item in candidates:
        await seed_authenticated_raw_mail(repository, item)


def test_purchase_references_are_conservatively_normalized_and_bounded() -> None:
    item = PurchaseDocumentCandidate(
        account_id="account-a",
        raw_mail_id=digest("raw"),
        raw_content_sha256=digest("content"),
        order_reference="  ORDER   １２３-A  ",  # noqa: RUF001 - tests NFKC normalization
        invoice_reference=" Invoice / 9 ",
    )

    assert item.order_reference == "order 123-a"
    assert item.invoice_reference == "invoice / 9"
    with pytest.raises(ValidationError, match="control characters"):
        PurchaseDocumentCandidate(
            account_id="account-a",
            raw_mail_id=digest("raw-control"),
            raw_content_sha256=digest("content-control"),
            order_reference="bad\nreference",
        )


def test_transport_retry_is_idempotent_but_revised_documents_append() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("purchase-owner")
        order = candidate(
            account.id,
            "order",
            kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
            order_reference="ORDER-123",
        )
        receipt = candidate(
            account.id,
            "receipt",
            kind=PurchaseDocumentKind.FINAL_RECEIPT,
            order_reference="order-123",
            invoice_reference="invoice-456",
        )
        revised = candidate(
            account.id,
            "revised-receipt",
            kind=PurchaseDocumentKind.FINAL_RECEIPT,
            invoice_reference="INVOICE-456",
        )
        await seed(repository, order, receipt, revised)

        first = await repository.attach_purchase_document(order)
        retry = await repository.attach_purchase_document(order)
        second = await repository.attach_purchase_document(receipt)
        third = await repository.attach_purchase_document(revised)

        assert first.duplicate is False
        assert retry.duplicate is True
        assert retry.document == first.document
        assert retry.purchase.revision_count == 1
        assert {first.purchase.id, second.purchase.id, third.purchase.id} == {first.purchase.id}
        assert [first.document.revision_number, second.document.revision_number] == [1, 2]
        assert third.document.revision_number == third.purchase.revision_count == 3

    asyncio.run(scenario())


def test_purchase_attachment_requires_matching_immutable_authentication() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("purchase-auth-owner")
        item = candidate(account.id, "purchase-auth", order_reference="order-123")
        await repository.seed_published_raw_mail(
            account_id=item.account_id,
            raw_mail_id=item.raw_mail_id,
            content_sha256=item.raw_content_sha256,
        )

        with pytest.raises(RawMailNotFound):
            await repository.attach_purchase_document(item)

        untrusted = RawMailAuthentication(
            id=item.raw_mail_id,
            account_id=item.account_id,
            raw_mail_id=item.raw_mail_id,
            raw_content_sha256=item.raw_content_sha256,
            outcome=RawMailAuthenticationOutcome.UNTRUSTED,
            verifier_version="test-aligned-dkim-v1",
        )
        recorded = await repository.record_raw_mail_authentication(untrusted)
        retry = await repository.record_raw_mail_authentication(untrusted)
        assert retry == recorded
        with pytest.raises(RawMailNotFound):
            await repository.attach_purchase_document(item)
        with pytest.raises(RawMailAuthenticationConflict):
            await repository.record_raw_mail_authentication(
                trusted_authentication_for_candidate(item)
            )

    asyncio.run(scenario())


def test_missing_or_merely_similar_identifiers_never_merge() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("similar-owner")
        missing_a = candidate(account.id, "same-date-total-a")
        missing_b = candidate(account.id, "same-date-total-b")
        distinct_order_a = candidate(account.id, "order-a", order_reference="order-100")
        distinct_order_b = candidate(account.id, "order-b", order_reference="order-101")
        await seed(repository, missing_a, missing_b, distinct_order_a, distinct_order_b)

        results = [
            await repository.attach_purchase_document(item)
            for item in (missing_a, missing_b, distinct_order_a, distinct_order_b)
        ]

        assert len({result.purchase.id for result in results}) == 4
        assert all(result.purchase.revision_count == 1 for result in results)

    asyncio.run(scenario())


def test_conflicting_bridge_identifiers_fail_without_merging_lifecycles() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("conflict-owner")
        order = candidate(account.id, "conflict-order", order_reference="order-a")
        invoice = candidate(account.id, "conflict-invoice", invoice_reference="invoice-b")
        bridge = candidate(
            account.id,
            "conflict-bridge",
            order_reference="order-a",
            invoice_reference="invoice-b",
        )
        await seed(repository, order, invoice, bridge)
        order_result = await repository.attach_purchase_document(order)
        invoice_result = await repository.attach_purchase_document(invoice)

        with pytest.raises(PurchaseIdentityConflict):
            await repository.attach_purchase_document(bridge)

        assert order_result.purchase.id != invoice_result.purchase.id
        assert (account.id, bridge.raw_mail_id) not in repository._purchase_documents
        assert repository._purchases[(account.id, order_result.purchase.id)].revision_count == 1
        assert repository._purchases[(account.id, invoice_result.purchase.id)].revision_count == 1

    asyncio.run(scenario())


def test_changed_retry_and_cross_account_raw_mail_fail_closed() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account_a = await repository.provision_account("raw-owner-a")
        account_b = await repository.provision_account("raw-owner-b")
        original = candidate(account_a.id, "raw-conflict", order_reference="order-a")
        await seed(repository, original)
        await repository.attach_purchase_document(original)

        with pytest.raises(PurchaseDocumentConflict):
            await repository.attach_purchase_document(
                original.model_copy(update={"order_reference": "order-b"})
            )
        with pytest.raises(RawMailNotFound):
            await repository.attach_purchase_document(
                original.model_copy(update={"account_id": account_b.id})
            )

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_transaction_connects_only_exact_business_aliases() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-purchase-identity-test"
        database = AsyncClient(project=project_id)
        account_id = "account-firestore"
        account_ref = database.collection("accounts").document(account_id)
        await account_ref.set({"status": "active"})
        order = candidate(
            account_id,
            "firestore-order",
            kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
            order_reference="ORDER-900",
        )
        receipt = candidate(
            account_id,
            "firestore-receipt",
            kind=PurchaseDocumentKind.FINAL_RECEIPT,
            order_reference="order-900",
            invoice_reference="invoice-901",
        )
        for item, transport_status in ((order, "published"), (receipt, "stored")):
            await (
                account_ref.collection("raw_mail")
                .document(item.raw_mail_id)
                .set(
                    {
                        "account_id": account_id,
                        "status": transport_status,
                        "content_sha256": item.raw_content_sha256,
                    }
                )
            )
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=database,
        )
        for item in (order, receipt):
            await repository.record_raw_mail_authentication(
                trusted_authentication_for_candidate(item)
            )

        first = await repository.attach_purchase_document(order)
        second = await repository.attach_purchase_document(receipt)
        retry = await repository.attach_purchase_document(receipt)
        confirmation_parsed = ParsedPurchaseDocument(
            parser_version="test-v1",
            kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
            items=[
                PurchaseItemDraft(
                    ordinal=1,
                    name="Synthetic apple",
                    normalized_name="synthetic apple",
                    disposition=PurchaseItemDisposition.ORDERED,
                    quantity=2,
                    unit_price_ore=450,
                    line_total_ore=900,
                )
            ],
            charges=[
                PurchaseChargeDraft(
                    kind=PurchaseChargeKind.TOTAL,
                    amount_ore=900,
                    description="Total",
                )
            ],
        )
        final_parsed = confirmation_parsed.model_copy(
            update={
                "kind": PurchaseDocumentKind.FINAL_RECEIPT,
                "items": [
                    confirmation_parsed.items[0].model_copy(
                        update={"disposition": PurchaseItemDisposition.DELIVERED}
                    )
                ],
            }
        )
        confirmation_normalization = await repository.normalize_purchase_document(
            document=first.document,
            parsed=confirmation_parsed,
        )
        final_normalization = await repository.normalize_purchase_document(
            document=second.document,
            parsed=final_parsed,
        )
        normalization_retry = await repository.normalize_purchase_document(
            document=second.document,
            parsed=final_parsed,
        )

        assert first.purchase.id == second.purchase.id == retry.purchase.id
        assert second.purchase.revision_count == retry.purchase.revision_count == 2
        assert retry.duplicate is True
        assert confirmation_normalization.duplicate is False
        assert final_normalization.duplicate is False
        assert normalization_retry.duplicate is True
        assert final_normalization.reconciliation.unresolved_item_count == 0
        assert final_normalization.reconciliation.items[0].disposition == (
            PurchaseReconciliationDisposition.DELIVERED_AS_ORDERED
        )
        order_alias_id = purchase_identity_alias_id(
            merchant="nemlig",
            kind="order",
            reference="order-900",
        )
        alias = await account_ref.collection("purchase_identities").document(order_alias_id).get()
        assert alias.exists
        assert alias.get("purchase_id") == first.purchase.id
        assert alias.get("reference_hash") == digest("order-900")
        assert "order-900" not in alias.to_dict().values()
        documents = [
            snapshot async for snapshot in account_ref.collection("purchase_documents").stream()
        ]
        assert {snapshot.id for snapshot in documents} == {
            order.raw_mail_id,
            receipt.raw_mail_id,
        }
        normalizations = [
            snapshot
            async for snapshot in account_ref.collection("purchase_normalizations").stream()
        ]
        persisted_items = [
            snapshot async for snapshot in account_ref.collection("purchase_items").stream()
        ]
        assert len(normalizations) == 2
        assert len(persisted_items) == 2
        database.close()

    asyncio.run(scenario())
