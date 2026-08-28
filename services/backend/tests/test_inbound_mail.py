import asyncio
from collections.abc import Iterator
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from foodlog_backend.app import create_app
from foodlog_backend.errors import (
    InboundAddressCollision,
    InboundAddressGenerationFailed,
    InboundAddressStateConflict,
)
from foodlog_backend.inbound_mail import (
    InboundMailAddressService,
    inbound_recipient_hash,
    normalize_inbound_mail_domain,
    normalize_inbound_recipient,
)
from foodlog_backend.models import InboundMailAddressStatus
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.settings import Settings

DOMAIN = "gemini-foodlog-2026.appspotmail.com"
TOKEN_A = "a" * 48
TOKEN_B = "b" * 48


def token_factory(tokens: list[str]) -> Iterator[str]:
    return iter(tokens)


def test_recipient_normalization_accepts_only_opaque_project_addresses() -> None:
    address = f"f-{TOKEN_A}@{DOMAIN}"

    assert normalize_inbound_mail_domain(f" {DOMAIN.upper()}. ") == DOMAIN
    assert normalize_inbound_recipient(f" {address.upper()} ", expected_domain=DOMAIN) == address
    assert (
        inbound_recipient_hash(address, expected_domain=DOMAIN)
        == sha256(address.encode()).hexdigest()
    )

    for invalid in (
        f"owner-account-id@{DOMAIN}",
        f"f-short@{DOMAIN}",
        f"f-{TOKEN_A}@attacker.example",
        f"f-{TOKEN_A}@@{DOMAIN}",
    ):
        with pytest.raises(ValueError, match="inbound recipient is invalid"):
            normalize_inbound_recipient(invalid, expected_domain=DOMAIN)


def test_address_generation_is_unique_opaque_and_idempotent_per_account() -> None:
    repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    tokens = token_factory([TOKEN_A, TOKEN_B, "c" * 48])
    service = InboundMailAddressService(
        repository=repository,
        domain=DOMAIN,
        token_factory=lambda: next(tokens),
    )

    async def scenario():
        account_a = await repository.provision_account("owner-a")
        account_b = await repository.provision_account("owner-b")
        address_a = await service.get_or_create("owner-a")
        repeated_a = await service.get_or_create("owner-a")
        address_b = await service.get_or_create("owner-b")
        return account_a, account_b, address_a, repeated_a, address_b

    account_a, account_b, address_a, repeated_a, address_b = asyncio.run(scenario())

    assert address_a == repeated_a
    assert address_a.address == f"f-{TOKEN_A}@{DOMAIN}"
    assert address_b.address == f"f-{'c' * 48}@{DOMAIN}"
    assert address_a.address != address_b.address
    assert account_a.id not in address_a.address
    assert account_b.id not in address_b.address
    assert "owner-a" not in address_a.address
    assert set(repository._inbound_mail_routes) == {
        sha256(address_a.address.encode()).hexdigest(),
        sha256(address_b.address.encode()).hexdigest(),
    }
    assert address_a.address not in repr(repository._inbound_mail_routes)


def test_collisions_retry_then_fail_without_cross_account_reassignment() -> None:
    repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    service = InboundMailAddressService(
        repository=repository,
        domain=DOMAIN,
        token_factory=lambda: TOKEN_A,
        max_attempts=2,
    )

    async def scenario() -> None:
        await repository.provision_account("owner-a")
        await repository.provision_account("owner-b")
        first = await service.get_or_create("owner-a")
        with pytest.raises(InboundAddressGenerationFailed):
            await service.get_or_create("owner-b")
        with pytest.raises(InboundAddressCollision):
            await repository.create_inbound_mail_address(
                owner_user_id="owner-b",
                address=first.address,
                address_hash=sha256(first.address.encode()).hexdigest(),
            )

    asyncio.run(scenario())


def test_inconsistent_route_state_fails_closed() -> None:
    repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    service = InboundMailAddressService(
        repository=repository,
        domain=DOMAIN,
        token_factory=lambda: TOKEN_A,
    )

    async def scenario() -> None:
        account = await repository.provision_account("owner-a")
        address = await service.get_or_create("owner-a")
        del repository._inbound_mail_routes[sha256(address.address.encode()).hexdigest()]
        assert account.id in repository._inbound_mail_addresses
        with pytest.raises(InboundAddressStateConflict):
            await service.get_or_create("owner-a")

    asyncio.run(scenario())


def test_authenticated_api_returns_one_private_address_per_account() -> None:
    settings = Settings(environment="test", inbound_mail_domain=DOMAIN)
    with TestClient(create_app(settings)) as client:
        missing_auth = client.post("/v1/inbound-mail-address")
        unprovisioned = client.post(
            "/v1/inbound-mail-address",
            headers={"X-FoodLog-Local-User": "owner-a"},
        )
        client.post("/v1/accounts", headers={"X-FoodLog-Local-User": "owner-a"})
        first = client.post(
            "/v1/inbound-mail-address",
            headers={"X-FoodLog-Local-User": "owner-a"},
        )
        repeated = client.post(
            "/v1/inbound-mail-address",
            headers={"X-FoodLog-Local-User": "owner-a"},
        )

    assert missing_auth.status_code == 401
    assert unprovisioned.status_code == 404
    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["address"].endswith(f"@{DOMAIN}")
    assert first.json()["generation"] == 1
    assert first.json()["revoked_at"] is None


def test_owner_can_revoke_and_atomically_replace_the_current_address() -> None:
    repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    tokens = token_factory([TOKEN_A, TOKEN_B, TOKEN_A])
    service = InboundMailAddressService(
        repository=repository,
        domain=DOMAIN,
        token_factory=lambda: next(tokens),
    )

    async def scenario():
        await repository.provision_account("owner-a")
        original = await service.get_or_create("owner-a")
        revoked = await service.revoke("owner-a", expected_generation=1)
        repeated_revoke = await service.revoke("owner-a", expected_generation=1)
        replacement = await service.rotate("owner-a", expected_generation=1)
        with pytest.raises(InboundAddressStateConflict):
            await service.rotate("owner-a", expected_generation=1)
        return original, revoked, repeated_revoke, replacement

    original, revoked, repeated_revoke, replacement = asyncio.run(scenario())

    assert original.address == f"f-{TOKEN_A}@{DOMAIN}"
    assert revoked == repeated_revoke
    assert revoked.status == InboundMailAddressStatus.REVOKED
    assert revoked.revoked_at is not None
    assert replacement.address == f"f-{TOKEN_B}@{DOMAIN}"
    assert replacement.status == InboundMailAddressStatus.ACTIVE
    assert replacement.generation == 2
    assert replacement.revoked_at is None
    original_route = repository._inbound_mail_routes[
        sha256(original.address.encode()).hexdigest()
    ]
    replacement_route = repository._inbound_mail_routes[
        sha256(replacement.address.encode()).hexdigest()
    ]
    assert original_route.status == InboundMailAddressStatus.REVOKED
    assert original_route.revoked_at == revoked.revoked_at
    assert replacement_route.status == InboundMailAddressStatus.ACTIVE
    assert replacement_route.generation == 2


def test_authenticated_api_exposes_revocation_and_rotation_lifecycle() -> None:
    settings = Settings(environment="test", inbound_mail_domain=DOMAIN)
    headers = {"X-FoodLog-Local-User": "owner-a"}
    with TestClient(create_app(settings)) as client:
        client.post("/v1/accounts", headers=headers)
        original = client.post("/v1/inbound-mail-address", headers=headers)
        revoked = client.post(
            "/v1/inbound-mail-address/revoke",
            headers=headers,
            json={"expected_generation": original.json()["generation"]},
        )
        repeated_revoke = client.post(
            "/v1/inbound-mail-address/revoke",
            headers=headers,
            json={"expected_generation": original.json()["generation"]},
        )
        rotated = client.post(
            "/v1/inbound-mail-address/rotate",
            headers=headers,
            json={"expected_generation": revoked.json()["generation"]},
        )
        stale = client.post(
            "/v1/inbound-mail-address/rotate",
            headers=headers,
            json={"expected_generation": original.json()["generation"]},
        )
        unauthenticated = client.post(
            "/v1/inbound-mail-address/revoke",
            json={"expected_generation": rotated.json()["generation"]},
        )

    assert original.status_code == revoked.status_code == repeated_revoke.status_code == 200
    assert revoked.json() == repeated_revoke.json()
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_at"] is not None
    assert rotated.status_code == 200
    assert rotated.json()["status"] == "active"
    assert rotated.json()["generation"] == original.json()["generation"] + 1
    assert rotated.json()["address"] != original.json()["address"]
    assert rotated.headers["cache-control"] == "no-store"
    assert stale.status_code == 409
    assert stale.json() == {"detail": "inbound_address_state_conflict"}
    assert unauthenticated.status_code == 401
