from __future__ import annotations

import asyncio
from hashlib import sha256

from foodlog_agent.context_tools import build_context_tools
from foodlog_agent.event_evidence_tool import ACCOUNT_ID_STATE_KEY
from foodlog_backend.models import (
    ParsedPurchaseDocument,
    PurchaseChargeDraft,
    PurchaseChargeKind,
    PurchaseDocumentCandidate,
    PurchaseDocumentKind,
    PurchaseItemDisposition,
    PurchaseItemDraft,
)
from foodlog_backend.repository import InMemoryRepository


class StateContext:
    def __init__(self, account_id: str) -> None:
        self.state = {ACCOUNT_ID_STATE_KEY: account_id}


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def item(
    ordinal: int,
    name: str,
    *,
    disposition: PurchaseItemDisposition,
    quantity: int = 1,
    line_total_ore: int = 1_000,
) -> PurchaseItemDraft:
    return PurchaseItemDraft(
        ordinal=ordinal,
        name=name,
        normalized_name=name.casefold(),
        disposition=disposition,
        quantity=quantity,
        category="Synthetic groceries",
        unit_description=f"{quantity} unit",
        unit_price_ore=line_total_ore // quantity,
        line_total_ore=line_total_ore,
    )


def test_recent_purchase_tool_prefers_delivery_and_preserves_uncertainty() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("purchase-tool-owner")
        foreign = await repository.provision_account("purchase-tool-foreign")
        confirmation = PurchaseDocumentCandidate(
            account_id=account.id,
            raw_mail_id=digest("purchase-tool-confirmation"),
            raw_content_sha256=digest("purchase-tool-confirmation-content"),
            kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
            order_reference="synthetic-order-700",
        )
        final = PurchaseDocumentCandidate(
            account_id=account.id,
            raw_mail_id=digest("purchase-tool-final"),
            raw_content_sha256=digest("purchase-tool-final-content"),
            kind=PurchaseDocumentKind.FINAL_RECEIPT,
            order_reference="synthetic-order-700",
            invoice_reference="synthetic-invoice-701",
        )
        for candidate in (confirmation, final):
            await repository.seed_published_raw_mail(
                account_id=account.id,
                raw_mail_id=candidate.raw_mail_id,
                content_sha256=candidate.raw_content_sha256,
            )
        confirmation_identity = await repository.attach_purchase_document(confirmation)
        final_identity = await repository.attach_purchase_document(final)
        await repository.normalize_purchase_document(
            document=confirmation_identity.document,
            parsed=ParsedPurchaseDocument(
                parser_version="purchase-tool-confirmation-v1",
                kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
                items=[
                    item(1, "Synthetic beef steak", disposition=PurchaseItemDisposition.ORDERED),
                    item(2, "Synthetic apples", disposition=PurchaseItemDisposition.ORDERED),
                ],
                charges=[
                    PurchaseChargeDraft(
                        kind=PurchaseChargeKind.TOTAL,
                        amount_ore=2_000,
                        description="Synthetic ordered total",
                    )
                ],
            ),
        )
        await repository.normalize_purchase_document(
            document=final_identity.document,
            parsed=ParsedPurchaseDocument(
                parser_version="purchase-tool-final-v1",
                kind=PurchaseDocumentKind.FINAL_RECEIPT,
                items=[
                    item(
                        1,
                        "Synthetic beef steak",
                        disposition=PurchaseItemDisposition.DELIVERED,
                    ),
                    item(
                        2,
                        "Synthetic chicken breast",
                        disposition=PurchaseItemDisposition.DELIVERED,
                        quantity=2,
                        line_total_ore=2_000,
                    ),
                ],
                charges=[
                    PurchaseChargeDraft(
                        kind=PurchaseChargeKind.TOTAL,
                        amount_ore=3_000,
                        description="Synthetic delivered total",
                    )
                ],
                included_vat_ore=600,
            ),
        )

        tools = {tool.name: tool for tool in build_context_tools(repository=repository)}
        result = await tools["get_recent_purchases"].run_async(  # type: ignore[arg-type]
            args={},
            tool_context=StateContext(account.id),
        )
        unavailable = await tools["get_recent_purchases"].run_async(  # type: ignore[arg-type]
            args={},
            tool_context=StateContext(foreign.id),
        )

        assert result["available"] is True
        assert result["unavailable_reason"] is None
        assert len(result["purchases"]) == 1
        purchase = result["purchases"][0]
        assert purchase["purchase_id"] == confirmation_identity.purchase.id
        assert purchase["evidence_status"] == "delivered"
        assert purchase["latest_total_ore"] == 3_000
        assert [item["name"] for item in purchase["items"]] == [
            "Synthetic beef steak",
            "Synthetic chicken breast",
        ]
        assert {item["disposition"] for item in purchase["items"]} == {"delivered"}
        assert [source["revision_number"] for source in purchase["source_documents"]] == [
            1,
            2,
        ]
        reconciliation = purchase["reconciliation"]
        assert reconciliation["unresolved_item_count"] == 2
        assert reconciliation["has_unresolved_substitution_pairing"] is True
        assert {item["disposition"] for item in reconciliation["items"]} == {
            "delivered_as_ordered",
            "removed_or_unresolved",
            "added_or_unresolved_substitution",
        }
        serialized = repr(result)
        for internal_field in (
            "account_id",
            "raw_content_sha256",
            "normalization_hash",
            "object_key",
        ):
            assert internal_field not in serialized
        assert unavailable == {
            "schema_version": "agent-context-v1",
            "available": False,
            "unavailable_reason": "no_purchase_evidence",
            "purchases": [],
        }

    asyncio.run(scenario())
