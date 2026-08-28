from __future__ import annotations

import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from enum import StrEnum
from hashlib import sha256
from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict, model_validator

from .models import (
    PurchaseDocumentCandidate,
    PurchaseDocumentKind,
    RawMailAuthentication,
    RawMailAuthenticationOutcome,
)

MAX_RAW_MAIL_BYTES = 20 * 1024 * 1024
MAX_CLASSIFIER_TEXT_CHARS = 250_000
CLASSIFIER_VERSION = "nemlig-structural-v1"
NEMLIG_SENDER = "kontakt@nemlig.com"
ORDER_SUBJECT = "tak for din ordre"
INVOICE_SUBJECT_PATTERN = re.compile(r"^faktura\s*-\s*(\d{4,20})$", re.IGNORECASE)
ORDER_REFERENCE_PATTERN = re.compile(r"ordrenummer\s*:\s*(\d{4,20})", re.IGNORECASE)


class MailClassificationOutcome(StrEnum):
    PURCHASE_DOCUMENT = "purchase_document"
    NOT_TRUSTED_NEMLIG = "not_trusted_nemlig"
    UNSUPPORTED_NEMLIG = "unsupported_nemlig"


class PurchaseMailClassification(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: MailClassificationOutcome
    classifier_version: str = CLASSIFIER_VERSION
    evidence_codes: tuple[str, ...]
    candidate: PurchaseDocumentCandidate | None = None

    @model_validator(mode="after")
    def candidate_matches_outcome(self) -> PurchaseMailClassification:
        if (self.candidate is not None) != (
            self.outcome == MailClassificationOutcome.PURCHASE_DOCUMENT
        ):
            raise ValueError("only purchase-document classifications contain a candidate")
        return self


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style"}:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag.casefold() in {
            "br",
            "div",
            "li",
            "p",
            "td",
            "th",
            "tr",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif not self.hidden_depth and tag.casefold() in {
            "div",
            "li",
            "p",
            "td",
            "th",
            "tr",
        }:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def raw_mail_object_key(*, account_id: str, mail_id: str) -> str:
    if not account_id or "/" in account_id or "\\" in account_id:
        raise ValueError("invalid account ID for raw mail")
    if re.fullmatch(r"[0-9a-f]{64}", mail_id) is None:
        raise ValueError("invalid raw mail ID")
    return f"accounts/{account_id}/raw-mail/{mail_id}.eml"


def _normalized_subject(message: Message) -> str:
    return " ".join(str(message.get("Subject") or "").casefold().split())


def _sender_address(message: Message) -> str | None:
    values = message.get_all("From", [])
    if len(values) != 1:
        return None
    parsed = getaddresses([str(values[0])])
    if len(parsed) != 1:
        return None
    return parsed[0][1].strip().casefold() or None


def _decode_text_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def visible_message_text(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_filename():
            continue
        content_type = part.get_content_type().casefold()
        decoded = _decode_text_part(part)
        if content_type == "text/plain":
            plain.append(decoded)
        elif content_type == "text/html":
            parser = _VisibleTextParser()
            parser.feed(decoded)
            html.append("".join(parser.parts))
    return "\n".join(plain or html)[:MAX_CLASSIFIER_TEXT_CHARS]


def validated_invoice_pdf_attachment(message: Message, reference: str) -> bytes | None:
    expected_filename = f"faktura - {reference}.pdf"
    matches: list[bytes] = []
    for part in message.walk():
        filename = " ".join((part.get_filename() or "").casefold().split())
        if filename != expected_filename:
            continue
        if part.get_content_disposition() != "attachment":
            continue
        if part.get_content_type().casefold() not in {
            "application/pdf",
            "application/octet-stream",
        }:
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and payload.startswith(b"%PDF-"):
            matches.append(payload)
    return matches[0] if len(matches) == 1 else None


def classify_nemlig_purchase_email(
    raw_message: bytes,
    *,
    account_id: str,
    mail_id: str,
    authentication: RawMailAuthentication | None = None,
) -> PurchaseMailClassification:
    if not raw_message or len(raw_message) > MAX_RAW_MAIL_BYTES:
        raise ValueError("raw mail exceeds the accepted classifier boundary")
    raw_content_sha256 = sha256(raw_message).hexdigest()
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    if message.defects:
        raise ValueError("raw mail is malformed")
    if (
        _sender_address(message) != NEMLIG_SENDER
        or authentication is None
        or authentication.account_id != account_id
        or authentication.raw_mail_id != mail_id
        or authentication.raw_content_sha256 != raw_content_sha256
        or authentication.outcome
        != RawMailAuthenticationOutcome.ALIGNED_DKIM_PASS
    ):
        return PurchaseMailClassification(
            outcome=MailClassificationOutcome.NOT_TRUSTED_NEMLIG,
            evidence_codes=("sender_or_authentication_not_trusted",),
        )

    subject = _normalized_subject(message)
    body = visible_message_text(message)
    if subject == ORDER_SUBJECT:
        match = ORDER_REFERENCE_PATTERN.search(body)
        if match and "tak for din ordre" in body.casefold():
            return PurchaseMailClassification(
                outcome=MailClassificationOutcome.PURCHASE_DOCUMENT,
                evidence_codes=(
                    "cryptographically_verified_aligned_dkim",
                    "order_confirmation_subject",
                    "retailer_labelled_order_reference",
                ),
                candidate=PurchaseDocumentCandidate(
                    account_id=account_id,
                    raw_mail_id=mail_id,
                    raw_content_sha256=raw_content_sha256,
                    kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
                    order_reference=match.group(1),
                ),
            )
        return PurchaseMailClassification(
            outcome=MailClassificationOutcome.UNSUPPORTED_NEMLIG,
            evidence_codes=("order_confirmation_missing_reference",),
        )

    invoice_match = INVOICE_SUBJECT_PATTERN.fullmatch(subject)
    if invoice_match:
        reference = invoice_match.group(1)
        if (
            "din ordre er på vej" in body.casefold()
            and "faktura" in body.casefold()
            and validated_invoice_pdf_attachment(message, reference) is not None
        ):
            return PurchaseMailClassification(
                outcome=MailClassificationOutcome.PURCHASE_DOCUMENT,
                evidence_codes=(
                    "cryptographically_verified_aligned_dkim",
                    "final_invoice_subject",
                    "matching_pdf_attachment",
                    "retailer_identifier_links_order_and_invoice",
                ),
                candidate=PurchaseDocumentCandidate(
                    account_id=account_id,
                    raw_mail_id=mail_id,
                    raw_content_sha256=raw_content_sha256,
                    kind=PurchaseDocumentKind.FINAL_RECEIPT,
                    order_reference=reference,
                    invoice_reference=reference,
                ),
            )
        return PurchaseMailClassification(
            outcome=MailClassificationOutcome.UNSUPPORTED_NEMLIG,
            evidence_codes=("invoice_missing_matching_pdf_or_body_marker",),
        )

    return PurchaseMailClassification(
        outcome=MailClassificationOutcome.UNSUPPORTED_NEMLIG,
        evidence_codes=("nemlig_message_is_not_a_purchase_document",),
    )
