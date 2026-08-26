from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from hashlib import sha256
from typing import Literal

LOCAL_PART_PATTERN = re.compile(r"^f-[0-9a-f]{48}$")
RAW_MAIL_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DOMAIN_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
MESSAGE_ID_PATTERN = re.compile(r"^<[^<>\s]{1,990}>$")
MAILBOX_PATTERN = re.compile(r"^[^\s<>@]{1,64}@[^\s<>@]{1,253}$")
MAX_HEADER_BYTES = 64 * 1024
MAX_HEADER_COUNT = 100
MAX_HEADER_LINE_BYTES = 998
MAX_PART_HEADER_COUNT = 30
MAX_PART_HEADER_VALUE_CHARS = 8 * 1024
MAX_MIME_PARTS = 100
MAX_MIME_DEPTH = 8
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_ENCODED_BYTES = 10 * 1024 * 1024
ALLOWED_MULTIPART_TYPES = frozenset(
    {"multipart/alternative", "multipart/mixed", "multipart/related"}
)
ALLOWED_LEAF_TYPES = frozenset(
    {
        "application/pdf",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/html",
        "text/plain",
    }
)
PDF_FALLBACK_CONTENT_TYPE = "application/octet-stream"
ALLOWED_TRANSFER_ENCODINGS = frozenset({"7bit", "8bit", "base64", "binary", "quoted-printable"})


class InvalidRecipient(ValueError):
    pass


class UnknownRecipient(ValueError):
    pass


class MailIdentityCollision(ValueError):
    pass


class InvalidRawMailObjectKey(ValueError):
    pass


class UnsafeMail(ValueError):
    """An external message that exceeds the deliberately accepted MIME surface."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_raw_mail_object_key(*, account_id: str, mail_id: str, object_key: str) -> None:
    if (
        not account_id
        or "/" in account_id
        or "\\" in account_id
        or RAW_MAIL_ID_PATTERN.fullmatch(mail_id) is None
        or object_key != f"accounts/{account_id}/raw-mail/{mail_id}.eml"
    ):
        raise InvalidRawMailObjectKey


def normalize_domain(value: str) -> str:
    domain = value.strip().casefold().rstrip(".")
    if len(domain) > 253 or DOMAIN_PATTERN.fullmatch(domain) is None:
        raise ValueError("inbound mail domain is invalid")
    return domain


def normalize_recipient(value: str, *, expected_domain: str) -> str:
    recipient = value.strip().casefold()
    if len(recipient) > 254 or recipient.count("@") != 1:
        raise InvalidRecipient
    local_part, domain = recipient.split("@", 1)
    if LOCAL_PART_PATTERN.fullmatch(local_part) is None or domain != normalize_domain(
        expected_domain
    ):
        raise InvalidRecipient
    return recipient


def recipient_hash(recipient: str, *, expected_domain: str) -> str:
    return sha256(
        normalize_recipient(recipient, expected_domain=expected_domain).encode()
    ).hexdigest()


def bounded_header(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())[:limit]
    return normalized or None


@dataclass(frozen=True)
class RawMailHeaders:
    sender: str | None
    sender_address: str
    subject: str | None
    message_id_hash: str | None


@dataclass(frozen=True)
class MimeInspection:
    headers: RawMailHeaders
    mime_part_count: int
    attachment_count: int
    content_types: tuple[str, ...]


def _header_block(raw_message: bytes) -> bytes:
    candidates = [
        position for marker in (b"\r\n\r\n", b"\n\n") if (position := raw_message.find(marker)) >= 0
    ]
    if not candidates:
        raise UnsafeMail("missing_header_terminator")
    block = raw_message[: min(candidates)]
    if len(block) > MAX_HEADER_BYTES:
        raise UnsafeMail("headers_too_large")
    lines = block.splitlines()
    if any(len(line) > MAX_HEADER_LINE_BYTES for line in lines):
        raise UnsafeMail("header_line_too_long")
    if b"\x00" in block:
        raise UnsafeMail("nul_in_headers")
    return block


def _validate_singleton_headers(message: Message) -> None:
    if len(list(message.raw_items())) > MAX_HEADER_COUNT:
        raise UnsafeMail("too_many_headers")
    for name in ("from", "message-id", "subject"):
        if len(message.get_all(name, [])) > 1:
            raise UnsafeMail(f"duplicate_{name}")


def _sender(message: Message) -> tuple[str | None, str]:
    raw_sender = message.get("From")
    if raw_sender is None:
        raise UnsafeMail("missing_from")
    parsed = getaddresses([str(raw_sender)])
    if len(parsed) != 1:
        raise UnsafeMail("ambiguous_from")
    _, address = parsed[0]
    normalized_address = address.strip().casefold()
    if len(normalized_address) > 320 or MAILBOX_PATTERN.fullmatch(normalized_address) is None:
        raise UnsafeMail("invalid_from")
    _, domain = normalized_address.rsplit("@", 1)
    try:
        normalize_domain(domain)
    except ValueError as error:
        raise UnsafeMail("invalid_from") from error
    return bounded_header(str(raw_sender), limit=500), normalized_address


def _walk_parts(message: Message, *, depth: int = 0) -> list[Message]:
    if depth > MAX_MIME_DEPTH:
        raise UnsafeMail("mime_too_deep")
    parts = [message]
    if message.is_multipart():
        payload = message.get_payload()
        if not isinstance(payload, list):
            raise UnsafeMail("invalid_multipart_payload")
        for child in payload:
            if not isinstance(child, Message):
                raise UnsafeMail("invalid_mime_part")
            parts.extend(_walk_parts(child, depth=depth + 1))
            if len(parts) > MAX_MIME_PARTS:
                raise UnsafeMail("too_many_mime_parts")
    return parts


def _validate_part_headers(part: Message) -> None:
    headers = list(part.raw_items())
    if len(headers) > MAX_PART_HEADER_COUNT:
        raise UnsafeMail("too_many_part_headers")
    for name in ("content-disposition", "content-transfer-encoding", "content-type"):
        if len(part.get_all(name, [])) > 1:
            raise UnsafeMail(f"duplicate_{name}")
    for name, value in headers:
        if len(name) > 78 or len(value) > MAX_PART_HEADER_VALUE_CHARS:
            raise UnsafeMail("part_header_too_large")


def inspect_untrusted_mime(raw_message: bytes) -> MimeInspection:
    """Validate structure only; all accepted message content remains untrusted data."""

    _header_block(raw_message)
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    if message.defects:
        raise UnsafeMail("malformed_mime")
    _validate_singleton_headers(message)
    sender, sender_address = _sender(message)
    message_id = bounded_header(message.get("Message-ID"), limit=998)
    if message_id is not None and MESSAGE_ID_PATTERN.fullmatch(message_id) is None:
        raise UnsafeMail("invalid_message_id")

    parts = _walk_parts(message)
    attachments = 0
    content_types: set[str] = set()
    for part in parts:
        if part.defects:
            raise UnsafeMail("malformed_mime")
        _validate_part_headers(part)
        content_type = part.get_content_type().casefold()
        content_types.add(content_type)
        if part.is_multipart():
            if content_type not in ALLOWED_MULTIPART_TYPES:
                raise UnsafeMail("unsupported_multipart_type")
            continue
        if content_type not in ALLOWED_LEAF_TYPES:
            if content_type != PDF_FALLBACK_CONTENT_TYPE:
                raise UnsafeMail("unsafe_or_unsupported_content_type")
            filename = part.get_filename() or ""
            decoded = part.get_payload(decode=True)
            if (
                part.get_content_disposition() != "attachment"
                or not filename.casefold().endswith(".pdf")
                or not isinstance(decoded, bytes)
                or not decoded.startswith(b"%PDF-")
            ):
                raise UnsafeMail("unsafe_octet_stream_attachment")
        transfer_encoding = (part.get("Content-Transfer-Encoding") or "7bit").strip().casefold()
        if transfer_encoding not in ALLOWED_TRANSFER_ENCODINGS:
            raise UnsafeMail("unsupported_transfer_encoding")
        disposition = part.get_content_disposition()
        if disposition not in (None, "attachment", "inline"):
            raise UnsafeMail("invalid_content_disposition")
        if disposition == "attachment":
            attachments += 1
            if attachments > MAX_ATTACHMENTS:
                raise UnsafeMail("too_many_attachments")
            payload = part.get_payload(decode=False)
            encoded_size = len(payload) if isinstance(payload, (bytes, str)) else 0
            if encoded_size > MAX_ATTACHMENT_ENCODED_BYTES:
                raise UnsafeMail("attachment_too_large")
        filename = part.get_filename()
        if filename is not None and (
            filename in (".", "..")
            or len(filename) > 255
            or any(c in filename for c in "\r\n\x00/\\")
        ):
            raise UnsafeMail("unsafe_attachment_name")

    return MimeInspection(
        headers=RawMailHeaders(
            sender=sender,
            sender_address=sender_address,
            subject=bounded_header(message.get("Subject"), limit=500),
            message_id_hash=(sha256(message_id.encode()).hexdigest() if message_id else None),
        ),
        mime_part_count=len(parts),
        attachment_count=attachments,
        content_types=tuple(sorted(content_types)),
    )


MailStatus = Literal["reserved", "stored", "published"]


@dataclass(frozen=True)
class RawMailRecord:
    id: str
    account_id: str
    recipient: str
    sender: str | None
    subject: str | None
    message_id_hash: str | None
    content_sha256: str
    size_bytes: int
    object_key: str
    sender_address: str | None = None
    trust_class: Literal["untrusted_external"] = "untrusted_external"
    mime_part_count: int = 0
    attachment_count: int = 0
    content_types: tuple[str, ...] = field(default_factory=tuple)
    status: MailStatus = "reserved"
    publish_attempt_count: int = 0
    provider_message_id: str | None = None
    stored_at: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class RawMailStoredEventV1:
    schema_version: Literal[1]
    kind: Literal["raw_mail_stored"]
    account_id: str
    mail_id: str
    trust_class: Literal["untrusted_external"] = "untrusted_external"

    def as_dict(self) -> dict[str, str | int]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "account_id": self.account_id,
            "mail_id": self.mail_id,
            "trust_class": self.trust_class,
        }
