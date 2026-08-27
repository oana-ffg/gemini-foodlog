import asyncio
from hashlib import sha256
from pathlib import Path

import pytest

from foodlog_backend.errors import PurchaseNormalizationConflict
from foodlog_backend.models import (
    PurchaseChargeKind,
    PurchaseDocumentCandidate,
    PurchaseDocumentKind,
    PurchaseItem,
    PurchaseItemDisposition,
    PurchaseReconciliationDisposition,
)
from foodlog_backend.purchase_normalization import (
    normalize_item_name,
    parse_invoice_rows,
    parse_money_ore,
    parse_order_confirmation,
    reconcile_purchase_items,
)
from foodlog_backend.repository import InMemoryRepository

FIXTURES = Path(__file__).parent / "fixtures" / "nemlig"


def persisted_item(
    *,
    document_id: str,
    ordinal: int,
    name: str,
    quantity: int,
    disposition: PurchaseItemDisposition,
) -> PurchaseItem:
    return PurchaseItem(
        id=sha256(f"{document_id}:{ordinal}".encode()).hexdigest(),
        account_id="account-a",
        purchase_id="purchase-a",
        document_id=document_id,
        document_revision_number=1 if disposition == PurchaseItemDisposition.ORDERED else 2,
        source_kind=(
            PurchaseDocumentKind.ORDER_CONFIRMATION
            if disposition == PurchaseItemDisposition.ORDERED
            else PurchaseDocumentKind.FINAL_RECEIPT
        ),
        ordinal=ordinal,
        name=name,
        normalized_name=normalize_item_name(name),
        disposition=disposition,
        quantity=quantity,
        unit_price_ore=100,
        line_total_ore=quantity * 100,
    )


def invoice_row(
    left: str = "",
    unit: str = "",
    discount: str = "",
    unit_price: str = "",
    quantity: str = "",
    price: str = "",
) -> tuple[str, str, str, str, str, str]:
    return left, unit, discount, unit_price, quantity, price


def test_order_confirmation_normalizes_items_and_charges() -> None:
    parsed = parse_order_confirmation((FIXTURES / "order-confirmation.eml").read_bytes())

    assert parsed.kind == PurchaseDocumentKind.ORDER_CONFIRMATION
    values = [
        (item.name, item.quantity, item.unit_price_ore, item.line_total_ore)
        for item in parsed.items
    ]
    assert values == [
        ("Syntetisk roedt aeble", 2, 450, 900),
        ("Syntetisk havredrik", 1, 1995, 1995),
    ]
    assert all(item.disposition == PurchaseItemDisposition.ORDERED for item in parsed.items)
    assert {charge.kind: charge.amount_ore for charge in parsed.charges} == {
        PurchaseChargeKind.ITEMS_SUBTOTAL: 2895,
        PurchaseChargeKind.DEPOSIT: 0,
        PurchaseChargeKind.PACKING_FEE: 750,
        PurchaseChargeKind.DELIVERY_FEE: 0,
        PurchaseChargeKind.CARD_FEE: 0,
        PurchaseChargeKind.TOTAL: 3645,
    }


def test_invoice_layout_normalizes_delivered_rows_wrapped_units_and_totals() -> None:
    rows = (
        invoice_row("Varer"),
        invoice_row("Frugt"),
        invoice_row(
            "Syntetisk rødt æble",
            "200 g",
            unit_price="4,50",
            quantity="2",
            price="9,00",
        ),
        invoice_row("Drikke"),
        invoice_row(
            "Syntetisk mandeldrik",
            "1 l / ex. pant",
            "2,00",
            "18,00",
            "1",
            "18,00",
        ),
        invoice_row("Pleje"),
        invoice_row(
            "Syntetisk næsespray",
            "1 mg",
            unit_price="59,95",
            quantity="1",
            price="59,95",
        ),
        invoice_row(unit="Xylometazolin"),
        invoice_row("Varer i alt", price="kr. 86,95"),
        invoice_row("Pant", price="kr. 0,00"),
        invoice_row("Pakkegebyr", "1 zone", price="kr. 7,50"),
        invoice_row("Fragt", price="kr. 0,00"),
        invoice_row("Total (heraf 25% moms kr. 18,89)", price="kr. 94,45"),
    )

    parsed = parse_invoice_rows(rows)

    assert parsed.kind == PurchaseDocumentKind.FINAL_RECEIPT
    assert [(item.name, item.category, item.quantity) for item in parsed.items] == [
        ("Syntetisk rødt æble", "Frugt", 2),
        ("Syntetisk mandeldrik", "Drikke", 1),
        ("Syntetisk næsespray", "Pleje", 1),
    ]
    assert parsed.items[1].included_discount_ore == 200
    assert parsed.items[2].unit_description == "1 mg Xylometazolin"
    assert parsed.included_vat_ore == 1889
    assert {charge.kind: charge.amount_ore for charge in parsed.charges} == {
        PurchaseChargeKind.ITEMS_SUBTOTAL: 8695,
        PurchaseChargeKind.DEPOSIT: 0,
        PurchaseChargeKind.PACKING_FEE: 750,
        PurchaseChargeKind.DELIVERY_FEE: 0,
        PurchaseChargeKind.TOTAL: 9445,
    }


def test_reconciliation_preserves_exact_changes_without_inventing_substitution_pairs() -> None:
    confirmation_id = "a" * 64
    final_id = "b" * 64
    confirmation = [
        persisted_item(
            document_id=confirmation_id,
            ordinal=1,
            name="Rødt æble",
            quantity=2,
            disposition=PurchaseItemDisposition.ORDERED,
        ),
        persisted_item(
            document_id=confirmation_id,
            ordinal=2,
            name="Havredrik",
            quantity=1,
            disposition=PurchaseItemDisposition.ORDERED,
        ),
        persisted_item(
            document_id=confirmation_id,
            ordinal=3,
            name="Banan",
            quantity=4,
            disposition=PurchaseItemDisposition.ORDERED,
        ),
    ]
    final = [
        persisted_item(
            document_id=final_id,
            ordinal=1,
            name="Rødt æble",
            quantity=3,
            disposition=PurchaseItemDisposition.DELIVERED,
        ),
        persisted_item(
            document_id=final_id,
            ordinal=2,
            name="Mandeldrik",
            quantity=1,
            disposition=PurchaseItemDisposition.DELIVERED,
        ),
        persisted_item(
            document_id=final_id,
            ordinal=3,
            name="Banan",
            quantity=4,
            disposition=PurchaseItemDisposition.DELIVERED,
        ),
    ]

    result = reconcile_purchase_items(
        account_id="account-a",
        purchase_id="purchase-a",
        confirmation_document_id=confirmation_id,
        confirmation_items=confirmation,
        final_document_id=final_id,
        final_items=final,
    )
    dispositions = {item.normalized_name: item.disposition for item in result.items}

    assert dispositions == {
        "banan": PurchaseReconciliationDisposition.DELIVERED_AS_ORDERED,
        "havredrik": PurchaseReconciliationDisposition.REMOVED_OR_UNRESOLVED,
        "mandeldrik": (PurchaseReconciliationDisposition.ADDED_OR_UNRESOLVED_SUBSTITUTION),
        "rødt æble": PurchaseReconciliationDisposition.QUANTITY_CHANGED,
    }
    assert result.unresolved_item_count == 2
    assert result.has_unresolved_substitution_pairing is True
    assert not any(
        item.confirmation_item_ids and item.final_item_ids
        for item in result.items
        if item.normalized_name in {"havredrik", "mandeldrik"}
    )


def test_danish_money_and_name_normalization_are_exact() -> None:
    assert parse_money_ore("kr. 1.229,70") == 122970
    assert parse_money_ore("0,00 kr") == 0
    assert normalize_item_name("  RØDT\u00a0 Æble  ") == "rødt æble"


def test_normalization_retry_is_idempotent_and_changed_payload_conflicts() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("normalization-owner")
        raw_message = (FIXTURES / "order-confirmation.eml").read_bytes()
        mail_id = sha256(raw_message).hexdigest()
        candidate = PurchaseDocumentCandidate(
            account_id=account.id,
            raw_mail_id=mail_id,
            raw_content_sha256=mail_id,
            kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
            order_reference="9000000001",
        )
        await repository.seed_published_raw_mail(
            account_id=account.id,
            raw_mail_id=mail_id,
            content_sha256=mail_id,
        )
        identity = await repository.attach_purchase_document(candidate)
        parsed = parse_order_confirmation(raw_message)

        first = await repository.normalize_purchase_document(
            document=identity.document,
            parsed=parsed,
        )
        retry = await repository.normalize_purchase_document(
            document=identity.document,
            parsed=parsed,
        )
        changed = parsed.model_copy(
            update={
                "items": [
                    parsed.items[0].model_copy(update={"quantity": 3}),
                    *parsed.items[1:],
                ]
            }
        )
        with pytest.raises(PurchaseNormalizationConflict):
            await repository.normalize_purchase_document(
                document=identity.document,
                parsed=changed,
            )

        assert first.duplicate is False
        assert retry.duplicate is True
        assert retry.normalization == first.normalization
        assert retry.reconciliation == first.reconciliation
        assert len(repository._purchase_normalizations) == 1

    asyncio.run(scenario())
