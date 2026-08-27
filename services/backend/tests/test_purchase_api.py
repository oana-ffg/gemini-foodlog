from __future__ import annotations

import asyncio
import os
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.app import create_app
from foodlog_backend.errors import PurchaseNotFound
from foodlog_backend.firestore_repository import FirestoreRepository
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
from foodlog_backend.settings import Settings


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def purchase_candidate(account_id: str, label: str) -> PurchaseDocumentCandidate:
    return PurchaseDocumentCandidate(
        account_id=account_id,
        raw_mail_id=digest(f"raw:{label}"),
        raw_content_sha256=digest(f"content:{label}"),
        kind=PurchaseDocumentKind.FINAL_RECEIPT,
        order_reference=f"order-{label}",
        invoice_reference=f"invoice-{label}",
    )


def parsed_receipt() -> ParsedPurchaseDocument:
    return ParsedPurchaseDocument(
        parser_version="purchase-api-test-v1",
        kind=PurchaseDocumentKind.FINAL_RECEIPT,
        items=[
            PurchaseItemDraft(
                ordinal=1,
                name="Synthetic red apple",
                normalized_name="synthetic red apple",
                disposition=PurchaseItemDisposition.DELIVERED,
                quantity=2,
                category="Synthetic produce",
                unit_description="2 units",
                unit_price_ore=450,
                line_total_ore=900,
            )
        ],
        charges=[
            PurchaseChargeDraft(
                kind=PurchaseChargeKind.TOTAL,
                amount_ore=900,
                description="Synthetic total",
            )
        ],
        included_vat_ore=180,
    )


async def seed_purchase(
    repository,
    owner_user_id: str,
    label: str,
    *,
    firestore_client: AsyncClient | None = None,
):
    account = await repository.provision_account(owner_user_id)
    candidate = purchase_candidate(account.id, label)
    if firestore_client is None:
        await repository.seed_published_raw_mail(
            account_id=account.id,
            raw_mail_id=candidate.raw_mail_id,
            content_sha256=candidate.raw_content_sha256,
        )
    else:
        await (
            firestore_client.collection("accounts")
            .document(account.id)
            .collection("raw_mail")
            .document(candidate.raw_mail_id)
            .set(
                {
                    "account_id": account.id,
                    "status": "published",
                    "content_sha256": candidate.raw_content_sha256,
                }
            )
        )
    identity = await repository.attach_purchase_document(candidate)
    await repository.normalize_purchase_document(
        document=identity.document,
        parsed=parsed_receipt(),
    )
    return identity.purchase


def test_purchase_api_returns_owner_scoped_sanitized_evidence() -> None:
    app = create_app(Settings(environment="test"))
    repository = app.state.container.repository

    async def prepare():
        owner_purchase = await seed_purchase(repository, "purchase-api-owner", "owner")
        foreign_purchase = await seed_purchase(repository, "purchase-api-foreign", "foreign")
        return owner_purchase, foreign_purchase

    owner_purchase, foreign_purchase = asyncio.run(prepare())
    owner_headers = {"X-FoodLog-Local-User": "purchase-api-owner"}
    foreign_headers = {"X-FoodLog-Local-User": "purchase-api-foreign"}

    with TestClient(app) as client:
        listed = client.get("/v1/purchases?limit=1", headers=owner_headers)
        detail = client.get(f"/v1/purchases/{owner_purchase.id}", headers=owner_headers)
        foreign_detail = client.get(
            f"/v1/purchases/{owner_purchase.id}", headers=foreign_headers
        )
        owner_reads_foreign = client.get(
            f"/v1/purchases/{foreign_purchase.id}", headers=owner_headers
        )
        invalid_limit = client.get("/v1/purchases?limit=0", headers=owner_headers)

    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "private, no-store"
    assert [item["id"] for item in listed.json()] == [owner_purchase.id]
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "private, no-store"
    body = detail.json()
    assert body["id"] == owner_purchase.id
    assert body["documents"][0]["normalization"]["parser_version"] == (
        "purchase-api-test-v1"
    )
    assert body["documents"][0]["items"][0]["name"] == "Synthetic red apple"
    assert body["documents"][0]["charges"][0]["amount_ore"] == 900
    assert body["reconciliation"]["items"][0]["delivered_quantity"] == 2
    serialized = detail.text
    assert "account_id" not in serialized
    assert "raw_content_sha256" not in serialized
    assert "normalization_hash" not in serialized
    assert "object_key" not in serialized
    assert foreign_detail.status_code == 404
    assert owner_reads_foreign.status_code == 404
    assert invalid_limit.status_code == 422


def test_in_memory_purchase_projection_orders_and_hides_foreign_purchases() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        first = await seed_purchase(repository, "projection-owner", "first")
        second = await seed_purchase(repository, "projection-owner", "second")
        foreign = await seed_purchase(repository, "projection-foreign", "foreign")

        purchases = await repository.list_purchases("projection-owner", limit=1)
        evidence = await repository.purchase_evidence_for_owner(
            "projection-owner", second.id
        )

        assert [purchase.id for purchase in purchases] == [second.id]
        assert evidence.purchase.id == second.id
        assert evidence.documents[0].purchase_id == second.id
        assert evidence.items[0].purchase_id == second.id
        assert evidence.reconciliation is not None
        with pytest.raises(PurchaseNotFound):
            await repository.purchase_evidence_for_owner("projection-owner", foreign.id)
        with pytest.raises(ValueError, match="between 1 and 50"):
            await repository.list_purchases("projection-owner", limit=51)
        assert first.id != second.id

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_purchase_projection_is_account_scoped() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-purchase-api-test"
        database = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=database,
        )
        owner_purchase = await seed_purchase(
            repository,
            "firestore-owner",
            "owner",
            firestore_client=database,
        )
        foreign_purchase = await seed_purchase(
            repository,
            "firestore-foreign",
            "foreign",
            firestore_client=database,
        )

        listed = await repository.list_purchases("firestore-owner", limit=20)
        evidence = await repository.purchase_evidence_for_owner(
            "firestore-owner", owner_purchase.id
        )
        recent_evidence = await repository.recent_purchase_evidence_for_account(
            account_id=owner_purchase.account_id,
            limit=5,
        )

        assert [purchase.id for purchase in listed] == [owner_purchase.id]
        assert evidence.purchase.id == owner_purchase.id
        assert len(evidence.documents) == len(evidence.normalizations) == 1
        assert len(evidence.items) == len(evidence.charges) == 1
        assert [bundle.purchase.id for bundle in recent_evidence] == [
            owner_purchase.id
        ]
        assert all(
            bundle.purchase.account_id == owner_purchase.account_id
            for bundle in recent_evidence
        )
        with pytest.raises(PurchaseNotFound):
            await repository.purchase_evidence_for_owner(
                "firestore-owner", foreign_purchase.id
            )
        database.close()

    asyncio.run(scenario())
