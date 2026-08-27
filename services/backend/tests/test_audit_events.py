from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.audit import build_audit_event
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import AuditAction, AuditActorKind, AuditSource


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_audit_events_are_immutable_idempotent_and_tenant_scoped() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-audit-contract-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        first_account = await repository.provision_account("audit-owner-a")
        second_account = await repository.provision_account("audit-owner-b")
        event = build_audit_event(
            account_id=first_account.id,
            action=AuditAction.CAPTURE_IMAGE_READ,
            actor_kind=AuditActorKind.USER,
            source=AuditSource.API,
            subject_kind="capture",
            subject_id="audit-capture-001",
        )

        stored = await repository.append_audit_event(event)
        retry = await repository.append_audit_event(
            build_audit_event(
                account_id=first_account.id,
                action=AuditAction.CAPTURE_IMAGE_READ,
                actor_kind=AuditActorKind.USER,
                source=AuditSource.API,
                subject_kind="capture",
                subject_id="audit-capture-001",
            )
        )

        assert stored == retry
        assert await repository.list_audit_events_for_owner("audit-owner-a") == [stored]
        assert await repository.list_audit_events_for_owner("audit-owner-b") == []
        assert stored.account_id != second_account.id

        conflicting = event.model_copy(update={"source": AuditSource.AGENT})
        with pytest.raises(
            ValueError,
            match="audit event identity conflicts with existing evidence",
        ):
            await repository.append_audit_event(conflicting)

        snapshot = (
            await client.collection("accounts")
            .document(first_account.id)
            .collection("audit_events")
            .document(event.id)
            .get()
        )
        assert snapshot.exists
        assert snapshot.get("action") == AuditAction.CAPTURE_IMAGE_READ.value
        assert "token" not in snapshot.to_dict()
        assert "payload" not in snapshot.to_dict()
        client.close()

    asyncio.run(scenario())
