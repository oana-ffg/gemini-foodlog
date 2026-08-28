from __future__ import annotations

import re
from collections.abc import Callable
from hashlib import sha256
from secrets import token_hex
from typing import Protocol

from .errors import InboundAddressCollision, InboundAddressGenerationFailed
from .models import InboundMailAddress

LOCAL_PART_PATTERN = re.compile(r"^f-[0-9a-f]{48}$")
DOMAIN_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


class InboundMailAddressRepository(Protocol):
    async def create_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        address: str,
        address_hash: str,
    ) -> InboundMailAddress: ...

    async def rotate_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        expected_generation: int,
        address: str,
        address_hash: str,
    ) -> InboundMailAddress: ...

    async def revoke_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        expected_generation: int,
    ) -> InboundMailAddress: ...


def normalize_inbound_mail_domain(value: str) -> str:
    domain = value.strip().casefold().rstrip(".")
    if len(domain) > 253 or DOMAIN_PATTERN.fullmatch(domain) is None:
        raise ValueError("inbound mail domain is invalid")
    return domain


def normalize_inbound_recipient(value: str, *, expected_domain: str) -> str:
    recipient = value.strip().casefold()
    if len(recipient) > 254 or recipient.count("@") != 1:
        raise ValueError("inbound recipient is invalid")
    local_part, domain = recipient.split("@", 1)
    if LOCAL_PART_PATTERN.fullmatch(local_part) is None:
        raise ValueError("inbound recipient is invalid")
    if domain != normalize_inbound_mail_domain(expected_domain):
        raise ValueError("inbound recipient is invalid")
    return recipient


def inbound_recipient_hash(value: str, *, expected_domain: str) -> str:
    normalized = normalize_inbound_recipient(value, expected_domain=expected_domain)
    return sha256(normalized.encode()).hexdigest()


class InboundMailAddressService:
    def __init__(
        self,
        *,
        repository: InboundMailAddressRepository,
        domain: str,
        token_factory: Callable[[], str] | None = None,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("inbound address generation requires at least one attempt")
        self._repository = repository
        self._domain = normalize_inbound_mail_domain(domain)
        self._token_factory = token_factory or (lambda: token_hex(24))
        self._max_attempts = max_attempts

    async def get_or_create(self, owner_user_id: str) -> InboundMailAddress:
        return await self._create_or_rotate(owner_user_id=owner_user_id)

    async def rotate(
        self,
        owner_user_id: str,
        *,
        expected_generation: int,
    ) -> InboundMailAddress:
        return await self._create_or_rotate(
            owner_user_id=owner_user_id,
            expected_generation=expected_generation,
        )

    async def revoke(
        self,
        owner_user_id: str,
        *,
        expected_generation: int,
    ) -> InboundMailAddress:
        return await self._repository.revoke_inbound_mail_address(
            owner_user_id=owner_user_id,
            expected_generation=expected_generation,
        )

    async def _create_or_rotate(
        self,
        *,
        owner_user_id: str,
        expected_generation: int | None = None,
    ) -> InboundMailAddress:
        for _ in range(self._max_attempts):
            token = self._token_factory()
            candidate = normalize_inbound_recipient(
                f"f-{token}@{self._domain}",
                expected_domain=self._domain,
            )
            try:
                arguments = {
                    "owner_user_id": owner_user_id,
                    "address": candidate,
                    "address_hash": inbound_recipient_hash(
                        candidate,
                        expected_domain=self._domain,
                    ),
                }
                if expected_generation is None:
                    return await self._repository.create_inbound_mail_address(**arguments)
                return await self._repository.rotate_inbound_mail_address(
                    expected_generation=expected_generation,
                    **arguments,
                )
            except InboundAddressCollision:
                continue
        raise InboundAddressGenerationFailed
