from datetime import datetime

from pydantic import BaseModel

from .models import (
    Purchase,
    PurchaseCharge,
    PurchaseChargeKind,
    PurchaseDocumentKind,
    PurchaseDocumentNormalization,
    PurchaseEvidenceBundle,
    PurchaseItem,
    PurchaseItemDisposition,
    PurchaseReconciledItem,
    PurchaseReconciliation,
    PurchaseReconciliationDisposition,
)


class PurchaseSummaryView(BaseModel):
    id: str
    merchant: str
    revision_count: int
    latest_confirmation_document_id: str | None
    latest_final_document_id: str | None
    created_at: datetime
    updated_at: datetime


class PurchaseNormalizationView(BaseModel):
    parser_version: str
    item_count: int
    charge_count: int
    included_vat_ore: int | None
    created_at: datetime


class PurchaseItemView(BaseModel):
    id: str
    ordinal: int
    name: str
    normalized_name: str
    disposition: PurchaseItemDisposition
    quantity: int
    category: str | None
    unit_description: str | None
    unit_price_ore: int
    included_discount_ore: int | None
    line_total_ore: int


class PurchaseChargeView(BaseModel):
    id: str
    kind: PurchaseChargeKind
    amount_ore: int
    description: str


class PurchaseDocumentView(BaseModel):
    id: str
    kind: PurchaseDocumentKind
    revision_number: int
    order_reference: str | None
    invoice_reference: str | None
    created_at: datetime
    normalization: PurchaseNormalizationView | None
    items: list[PurchaseItemView]
    charges: list[PurchaseChargeView]


class PurchaseReconciledItemView(BaseModel):
    id: str
    normalized_name: str
    display_name: str
    disposition: PurchaseReconciliationDisposition
    ordered_quantity: int | None
    delivered_quantity: int | None
    confirmation_item_ids: list[str]
    final_item_ids: list[str]


class PurchaseReconciliationView(BaseModel):
    confirmation_document_id: str | None
    final_document_id: str | None
    item_count: int
    unresolved_item_count: int
    has_unresolved_substitution_pairing: bool
    items: list[PurchaseReconciledItemView]
    updated_at: datetime


class PurchaseDetailView(PurchaseSummaryView):
    documents: list[PurchaseDocumentView]
    reconciliation: PurchaseReconciliationView | None


def purchase_summary_view(purchase: Purchase) -> PurchaseSummaryView:
    return PurchaseSummaryView.model_validate(purchase.model_dump(exclude={"account_id"}))


def _normalization_view(
    normalization: PurchaseDocumentNormalization,
) -> PurchaseNormalizationView:
    return PurchaseNormalizationView.model_validate(
        normalization.model_dump(
            exclude={
                "id",
                "account_id",
                "purchase_id",
                "document_id",
                "document_revision_number",
                "document_kind",
                "normalization_hash",
            }
        )
    )


def _item_view(item: PurchaseItem) -> PurchaseItemView:
    return PurchaseItemView.model_validate(
        item.model_dump(
            exclude={
                "account_id",
                "purchase_id",
                "document_id",
                "document_revision_number",
                "source_kind",
            }
        )
    )


def _charge_view(charge: PurchaseCharge) -> PurchaseChargeView:
    return PurchaseChargeView.model_validate(
        charge.model_dump(exclude={"account_id", "purchase_id", "document_id"})
    )


def _reconciled_item_view(item: PurchaseReconciledItem) -> PurchaseReconciledItemView:
    return PurchaseReconciledItemView.model_validate(item.model_dump())


def _reconciliation_view(
    reconciliation: PurchaseReconciliation,
) -> PurchaseReconciliationView:
    return PurchaseReconciliationView(
        confirmation_document_id=reconciliation.confirmation_document_id,
        final_document_id=reconciliation.final_document_id,
        item_count=reconciliation.item_count,
        unresolved_item_count=reconciliation.unresolved_item_count,
        has_unresolved_substitution_pairing=(reconciliation.has_unresolved_substitution_pairing),
        items=[_reconciled_item_view(item) for item in reconciliation.items],
        updated_at=reconciliation.updated_at,
    )


def purchase_detail_view(bundle: PurchaseEvidenceBundle) -> PurchaseDetailView:
    normalizations = {
        normalization.document_id: normalization for normalization in bundle.normalizations
    }
    items_by_document: dict[str, list[PurchaseItem]] = {}
    for item in bundle.items:
        items_by_document.setdefault(item.document_id, []).append(item)
    charges_by_document: dict[str, list[PurchaseCharge]] = {}
    for charge in bundle.charges:
        charges_by_document.setdefault(charge.document_id, []).append(charge)

    documents = []
    for document in bundle.documents:
        normalization = normalizations.get(document.id)
        document_items = sorted(
            items_by_document.get(document.id, []), key=lambda item: item.ordinal
        )
        document_charges = sorted(
            charges_by_document.get(document.id, []), key=lambda charge: charge.kind.value
        )
        documents.append(
            PurchaseDocumentView(
                id=document.id,
                kind=document.kind,
                revision_number=document.revision_number,
                order_reference=document.order_reference,
                invoice_reference=document.invoice_reference,
                created_at=document.created_at,
                normalization=(
                    _normalization_view(normalization) if normalization is not None else None
                ),
                items=[_item_view(item) for item in document_items],
                charges=[_charge_view(charge) for charge in document_charges],
            )
        )

    summary = purchase_summary_view(bundle.purchase)
    return PurchaseDetailView(
        **summary.model_dump(),
        documents=documents,
        reconciliation=(
            _reconciliation_view(bundle.reconciliation)
            if bundle.reconciliation is not None
            else None
        ),
    )
