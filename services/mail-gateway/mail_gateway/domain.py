from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.parser import BytesHeaderParser
from hashlib import sha256
from typing import Literal

LOCAL_PART_PATTERN = re.compile(r"^f-[0-9a-f]{48}$")
DOMAIN_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
MESSAGE_ID_PATTERN = re.compile(r"^<[^<>\s]{1,990}>$")


class InvalidRecipient(ValueError):
    pass


class UnknownRecipient(ValueError):
    pass


class MailIdentityCollision(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


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
    subject: str | None
    message_id_hash: str | None


def extract_bounded_headers(raw_message: bytes) -> RawMailHeaders:
    message = BytesHeaderParser(policy=policy.default).parsebytes(raw_message)
    message_id = bounded_header(message.get("Message-ID"), limit=998)
    normalized_message_id = (
        message_id if message_id and MESSAGE_ID_PATTERN.fullmatch(message_id) else None
    )
    return RawMailHeaders(
        sender=bounded_header(message.get("From"), limit=500),
        subject=bounded_header(message.get("Subject"), limit=500),
        message_id_hash=(
            sha256(normalized_message_id.encode()).hexdigest()
            if normalized_message_id is not None
            else None
        ),
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

    def as_dict(self) -> dict[str, str | int]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "account_id": self.account_id,
            "mail_id": self.mail_id,
        }
