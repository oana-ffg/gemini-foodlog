from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from email import policy
from email.message import Message
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
from unicodedata import normalize as unicode_normalize

import pdfplumber

from .models import (
    ParsedPurchaseDocument,
    PurchaseCharge,
    PurchaseChargeDraft,
    PurchaseChargeKind,
    PurchaseDocument,
    PurchaseDocumentKind,
    PurchaseDocumentNormalization,
    PurchaseItem,
    PurchaseItemDisposition,
    PurchaseItemDraft,
    PurchaseReconciledItem,
    PurchaseReconciliation,
    PurchaseReconciliationDisposition,
    utc_now,
)
from .purchase_mail import visible_message_text

PARSER_VERSION = "nemlig-purchase-v1"
MONEY_PATTERN = re.compile(
    r"^(?:kr\.?\s*)?(-?[0-9.]+,[0-9]{2})(?:\s*kr\.?)?$",
    re.IGNORECASE,
)
VAT_PATTERN = re.compile(r"heraf\s+25%\s+moms\s+kr\.\s*([0-9.]+,[0-9]{2})", re.IGNORECASE)


def purchase_item_id(document_id: str, ordinal: int) -> str:
    return sha256(f"purchase-item-v1\0{document_id}\0{ordinal}".encode()).hexdigest()


def purchase_charge_id(document_id: str, kind: PurchaseChargeKind) -> str:
    return sha256(f"purchase-charge-v1\0{document_id}\0{kind.value}".encode()).hexdigest()


def normalize_item_name(value: str) -> str:
    normalized = " ".join(unicode_normalize("NFKC", value).casefold().split())
    if not normalized or len(normalized) > 240:
        raise ValueError("purchase item name must contain 1-240 normalized characters")
    return normalized


def parse_money_ore(value: str) -> int:
    match = MONEY_PATTERN.fullmatch(" ".join(value.split()))
    if match is None:
        raise ValueError(f"unsupported DKK amount: {value!r}")
    normalized = match.group(1).replace(".", "").replace(",", ".")
    major, minor = normalized.split(".")
    amount = int(major) * 100 + int(minor)
    if amount < 0 or amount > 100_000_000:
        raise ValueError("DKK amount is outside the accepted range")
    return amount


def _message(raw_message: bytes) -> Message:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    if message.defects:
        raise ValueError("purchase MIME is malformed")
    return message


def _visible_lines(message: Message) -> list[str]:
    return [
        " ".join(line.split())
        for line in visible_message_text(message).splitlines()
        if line.strip()
    ]


def _charge_kind(label: str) -> PurchaseChargeKind | None:
    normalized = " ".join(label.casefold().split()).rstrip(":")
    return {
        "subtotal": PurchaseChargeKind.ITEMS_SUBTOTAL,
        "varer i alt": PurchaseChargeKind.ITEMS_SUBTOTAL,
        "pant": PurchaseChargeKind.DEPOSIT,
        "pakkegebyr": PurchaseChargeKind.PACKING_FEE,
        "fragt": PurchaseChargeKind.DELIVERY_FEE,
        "kortgebyr": PurchaseChargeKind.CARD_FEE,
        "total": PurchaseChargeKind.TOTAL,
    }.get(normalized)


def parse_order_confirmation(raw_message: bytes) -> ParsedPurchaseDocument:
    lines = _visible_lines(_message(raw_message))
    try:
        start = lines.index("Ordreoversigt")
        item_start = start + 4
        subtotal = lines.index("Subtotal", item_start)
    except ValueError as error:
        raise ValueError("order confirmation item table was not found") from error
    if lines[start + 1 : item_start] != ["Vare", "Antal", "Pris pr. stk"]:
        raise ValueError("order confirmation item headers changed")
    item_cells = lines[item_start:subtotal]
    if not item_cells or len(item_cells) % 4:
        raise ValueError("order confirmation item rows are incomplete")

    items = []
    for offset in range(0, len(item_cells), 4):
        name, quantity_text, unit_price_text, line_total_text = item_cells[offset : offset + 4]
        try:
            quantity = int(quantity_text)
        except ValueError as error:
            raise ValueError("order confirmation quantity is not an integer") from error
        items.append(
            PurchaseItemDraft(
                ordinal=len(items) + 1,
                name=name.removeprefix("* ").strip(),
                normalized_name=normalize_item_name(name.removeprefix("* ")),
                disposition=PurchaseItemDisposition.ORDERED,
                quantity=quantity,
                unit_price_ore=parse_money_ore(unit_price_text),
                line_total_ore=parse_money_ore(line_total_text),
            )
        )

    charges = []
    cursor = subtotal
    while cursor + 1 < len(lines):
        label = lines[cursor]
        kind = _charge_kind(label.split(":", 1)[0])
        if kind is None:
            break
        charges.append(
            PurchaseChargeDraft(
                kind=kind,
                amount_ore=parse_money_ore(lines[cursor + 1]),
                description=label,
            )
        )
        cursor += 2
        if kind == PurchaseChargeKind.TOTAL:
            break

    return ParsedPurchaseDocument(
        parser_version=PARSER_VERSION,
        kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
        items=items,
        charges=charges,
    )


def _invoice_pdf(message: Message) -> bytes:
    for part in message.walk():
        filename = " ".join((part.get_filename() or "").casefold().split())
        if not filename.startswith("faktura - ") or not filename.endswith(".pdf"):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and payload.startswith(b"%PDF-"):
            return payload
    raise ValueError("final invoice has no validated PDF attachment")


InvoiceRow = tuple[str, str, str, str, str, str]


def _group_words_by_row(words: list[dict]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda value: (value["top"], value["x0"])):
        if not rows or abs(word["top"] - rows[-1][0]["top"]) > 1.5:
            rows.append([word])
        else:
            rows[-1].append(word)
    return rows


def _header_right_edges(words: list[dict]) -> tuple[float, float, float, float, float]:
    by_text: dict[str, list[dict]] = defaultdict(list)
    for word in words:
        by_text[word["text"]].append(word)
    try:
        unit_right = by_text["Enhed"][0]["x1"]
        discount_right = by_text["rabat"][0]["x1"]
        unit_price_right = by_text["pris"][0]["x1"]
        quantity_right = by_text["Antal"][0]["x1"]
        line_total_right = by_text["Pris"][-1]["x1"]
    except (IndexError, KeyError) as error:
        raise ValueError("invoice table headers changed") from error
    edges = (
        unit_right,
        discount_right,
        unit_price_right,
        quantity_right,
        line_total_right,
    )
    if list(edges) != sorted(edges):
        raise ValueError("invoice table columns are not ordered")
    return edges


def _words_to_invoice_row(
    words: list[dict],
    edges: tuple[float, float, float, float, float],
) -> InvoiceRow:
    unit_right, discount_right, unit_price_right, quantity_right, line_total_right = edges
    cells: list[list[dict]] = [[] for _ in range(6)]
    prefix = []
    numeric_columns = (
        (2, discount_right),
        (3, unit_price_right),
        (4, quantity_right),
        (5, line_total_right),
    )
    for word in sorted(words, key=lambda value: value["x0"]):
        token = word["text"]
        if word["x0"] > quantity_right + 5:
            cells[5].append(word)
            continue
        if MONEY_PATTERN.fullmatch(token) or token.isdecimal():
            matches = [
                (abs(word["x1"] - right_edge), index)
                for index, right_edge in numeric_columns
                if abs(word["x1"] - right_edge) <= 16
            ]
            if matches:
                cells[min(matches)[1]].append(word)
                continue
        prefix.append(word)

    unit_start = len(prefix)
    if prefix and prefix[-1]["x1"] >= unit_right - 18:
        unit_start -= 1
        while unit_start > 0:
            gap = prefix[unit_start]["x0"] - prefix[unit_start - 1]["x1"]
            if gap > 12:
                break
            unit_start -= 1
    cells[0].extend(prefix[:unit_start])
    cells[1].extend(prefix[unit_start:])

    return tuple(
        " ".join(word["text"] for word in sorted(cell, key=lambda value: value["x0"]))
        for cell in cells
    )  # type: ignore[return-value]


def _invoice_rows(pdf: bytes) -> list[InvoiceRow]:
    try:
        with pdfplumber.open(BytesIO(pdf)) as document:
            if not 1 <= len(document.pages) <= 20:
                raise ValueError("invoice PDF page count is outside the accepted range")
            invoice_rows = []
            for page in document.pages:
                rows = _group_words_by_row(page.extract_words())
                header_index = next(
                    (
                        index
                        for index, row in enumerate(rows)
                        if {"Varekategori", "Enhed", "Indregnet", "rabat", "Stk.", "Antal", "Pris"}
                        <= {word["text"] for word in row}
                    ),
                    None,
                )
                if header_index is None:
                    raise ValueError("invoice table headers changed")
                edges = _header_right_edges(rows[header_index])
                invoice_rows.extend(
                    _words_to_invoice_row(row, edges) for row in rows[header_index + 1 :]
                )
    except Exception as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError("invoice PDF could not be parsed") from error
    return invoice_rows


def parse_invoice_rows(rows: Iterable[InvoiceRow]) -> ParsedPurchaseDocument:
    items: list[PurchaseItemDraft] = []
    charges_by_kind: dict[PurchaseChargeKind, PurchaseChargeDraft] = {}
    included_vat_ore = None
    current_category: str | None = None
    in_items = False
    for cells in rows:
        left, unit, discount, unit_price, quantity_text, line_total = cells
        if left == "Varer" and not any(cells[1:]):
            in_items = True
            continue
        if not any(cells):
            continue

        if left.casefold().startswith("varer i alt"):
            in_items = False
        if not in_items:
            label = left.split("(", 1)[0].strip()
            kind = _charge_kind(label)
            if kind is not None:
                amount_text = line_total.removeprefix("kr.").strip()
                charges_by_kind[kind] = PurchaseChargeDraft(
                    kind=kind,
                    amount_ore=parse_money_ore(amount_text),
                    description=left,
                )
                if kind == PurchaseChargeKind.TOTAL:
                    vat_match = VAT_PATTERN.search(left)
                    if vat_match:
                        included_vat_ore = parse_money_ore(vat_match.group(1))
            continue

        if quantity_text and line_total:
            if not left or not unit_price:
                raise ValueError("invoice item row is missing required cells")
            try:
                quantity = int(quantity_text)
            except ValueError as error:
                raise ValueError("invoice item quantity is not an integer") from error
            items.append(
                PurchaseItemDraft(
                    ordinal=len(items) + 1,
                    name=left,
                    normalized_name=normalize_item_name(left),
                    disposition=PurchaseItemDisposition.DELIVERED,
                    quantity=quantity,
                    category=current_category,
                    unit_description=unit or None,
                    unit_price_ore=parse_money_ore(unit_price),
                    included_discount_ore=(parse_money_ore(discount) if discount else None),
                    line_total_ore=parse_money_ore(line_total),
                )
            )
        elif left and not any(cells[1:]):
            current_category = left
        elif unit and not any((left, discount, unit_price, quantity_text, line_total)):
            if not items:
                raise ValueError("invoice has an orphaned wrapped unit")
            items[-1] = items[-1].model_copy(
                update={
                    "unit_description": " ".join(
                        filter(None, (items[-1].unit_description, unit))
                    )
                }
            )
        elif any(cells):
            raise ValueError("invoice contains an unrecognized table row")

    return ParsedPurchaseDocument(
        parser_version=PARSER_VERSION,
        kind=PurchaseDocumentKind.FINAL_RECEIPT,
        items=items,
        charges=list(charges_by_kind.values()),
        included_vat_ore=included_vat_ore,
    )


def parse_final_invoice(raw_message: bytes) -> ParsedPurchaseDocument:
    return parse_invoice_rows(_invoice_rows(_invoice_pdf(_message(raw_message))))


def parse_purchase_document(
    raw_message: bytes,
    *,
    kind: PurchaseDocumentKind,
) -> ParsedPurchaseDocument:
    if kind == PurchaseDocumentKind.ORDER_CONFIRMATION:
        return parse_order_confirmation(raw_message)
    if kind == PurchaseDocumentKind.FINAL_RECEIPT:
        return parse_final_invoice(raw_message)
    raise ValueError("unknown purchase documents cannot be normalized")


def materialize_purchase_document_normalization(
    *,
    document: PurchaseDocument,
    parsed: ParsedPurchaseDocument,
) -> tuple[PurchaseDocumentNormalization, list[PurchaseItem], list[PurchaseCharge]]:
    if parsed.kind != document.kind or parsed.kind == PurchaseDocumentKind.UNKNOWN:
        raise ValueError("parsed purchase kind does not match its source document")
    canonical = json.dumps(
        parsed.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    normalization_hash = sha256(canonical.encode()).hexdigest()
    created_at = utc_now()
    normalization = PurchaseDocumentNormalization(
        id=document.id,
        account_id=document.account_id,
        purchase_id=document.purchase_id,
        document_id=document.id,
        document_revision_number=document.revision_number,
        document_kind=document.kind,
        parser_version=parsed.parser_version,
        normalization_hash=normalization_hash,
        item_count=len(parsed.items),
        charge_count=len(parsed.charges),
        included_vat_ore=parsed.included_vat_ore,
        created_at=created_at,
    )
    items = [
        PurchaseItem(
            id=purchase_item_id(document.id, draft.ordinal),
            account_id=document.account_id,
            purchase_id=document.purchase_id,
            document_id=document.id,
            document_revision_number=document.revision_number,
            source_kind=document.kind,
            **draft.model_dump(),
        )
        for draft in parsed.items
    ]
    charges = [
        PurchaseCharge(
            id=purchase_charge_id(document.id, draft.kind),
            account_id=document.account_id,
            purchase_id=document.purchase_id,
            document_id=document.id,
            **draft.model_dump(),
        )
        for draft in parsed.charges
    ]
    return normalization, items, charges


def reconcile_purchase_items(
    *,
    account_id: str,
    purchase_id: str,
    confirmation_document_id: str | None,
    confirmation_items: list[PurchaseItem],
    final_document_id: str | None,
    final_items: list[PurchaseItem],
) -> PurchaseReconciliation:
    ordered: dict[str, list[PurchaseItem]] = defaultdict(list)
    delivered: dict[str, list[PurchaseItem]] = defaultdict(list)
    for item in confirmation_items:
        ordered[item.normalized_name].append(item)
    for item in final_items:
        delivered[item.normalized_name].append(item)

    reconciled = []
    ordered_only = set(ordered) - set(delivered)
    delivered_only = set(delivered) - set(ordered)
    for normalized_name in sorted(set(ordered) | set(delivered)):
        ordered_group = ordered.get(normalized_name, [])
        delivered_group = delivered.get(normalized_name, [])
        ordered_quantity = sum(item.quantity for item in ordered_group) or None
        delivered_quantity = sum(item.quantity for item in delivered_group) or None
        if ordered_group and delivered_group:
            disposition = (
                PurchaseReconciliationDisposition.DELIVERED_AS_ORDERED
                if ordered_quantity == delivered_quantity
                else PurchaseReconciliationDisposition.QUANTITY_CHANGED
            )
        elif ordered_group:
            disposition = PurchaseReconciliationDisposition.REMOVED_OR_UNRESOLVED
        else:
            disposition = (
                PurchaseReconciliationDisposition.ADDED_OR_UNRESOLVED_SUBSTITUTION
            )
        display_item = (delivered_group or ordered_group)[0]
        reconciled.append(
            PurchaseReconciledItem(
                id=sha256(f"{purchase_id}\0{normalized_name}".encode()).hexdigest(),
                normalized_name=normalized_name,
                display_name=display_item.name,
                disposition=disposition,
                ordered_quantity=ordered_quantity,
                delivered_quantity=delivered_quantity,
                confirmation_item_ids=[item.id for item in ordered_group],
                final_item_ids=[item.id for item in delivered_group],
            )
        )

    payload = "\n".join(
        (
            purchase_id,
            confirmation_document_id or "",
            final_document_id or "",
            *(item.model_dump_json() for item in reconciled),
        )
    )
    unresolved = sum(
        item.disposition
        in {
            PurchaseReconciliationDisposition.REMOVED_OR_UNRESOLVED,
            PurchaseReconciliationDisposition.ADDED_OR_UNRESOLVED_SUBSTITUTION,
        }
        for item in reconciled
    )
    return PurchaseReconciliation(
        account_id=account_id,
        purchase_id=purchase_id,
        confirmation_document_id=confirmation_document_id,
        final_document_id=final_document_id,
        reconciliation_hash=sha256(payload.encode()).hexdigest(),
        item_count=len(reconciled),
        unresolved_item_count=unresolved,
        has_unresolved_substitution_pairing=bool(ordered_only and delivered_only),
        items=reconciled,
    )
