from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_agent.event_evidence_tool import ACCOUNT_ID_STATE_KEY
from foodlog_agent.knowledge_tools import KnowledgeToolsService
from foodlog_backend.app import create_app
from foodlog_backend.errors import KnowledgePageNotFound
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.household_teaching import HouseholdTeachingService
from foodlog_backend.models import (
    KnowledgeLifecycle,
    StableKnowledgeCorrectionCreate,
    StableKnowledgeRetirementCreate,
    StableKnowledgeTeachingCreate,
    UserContextNoteStatus,
)
from foodlog_backend.settings import Settings

OWNER_HEADERS = {"X-FoodLog-Local-User": "teaching-owner"}
FOREIGN_HEADERS = {"X-FoodLog-Local-User": "teaching-foreign"}


class StateContext:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state


def test_stable_teaching_is_visible_to_agent_correctable_retireable_and_tenant_scoped() -> None:
    app = create_app(Settings(environment="test"))
    repository = app.state.container.repository
    with TestClient(app) as client:
        owner = client.post("/v1/accounts", headers=OWNER_HEADERS)
        foreign = client.post("/v1/accounts", headers=FOREIGN_HEADERS)
        assert owner.status_code == foreign.status_code == 200

        statement = "Meat placed in the air-fryer basket by the sink is usually steak."
        created = client.post(
            "/v1/knowledge",
            headers={**OWNER_HEADERS, "Idempotency-Key": "stable-teaching-create-0001"},
            json={"statement": statement},
        )
        exact_retry = client.post(
            "/v1/knowledge",
            headers={**OWNER_HEADERS, "Idempotency-Key": "stable-teaching-create-0001"},
            json={"statement": statement},
        )
        changed_retry = client.post(
            "/v1/knowledge",
            headers={**OWNER_HEADERS, "Idempotency-Key": "stable-teaching-create-0001"},
            json={"statement": "This is different wording."},
        )

        assert created.status_code == exact_retry.status_code == 201
        assert exact_retry.json() == created.json()
        assert changed_retry.status_code == 409
        created_payload = created.json()
        page_id = created_payload["page"]["id"]
        assert created_payload["revision"]["statement"] == statement
        assert created_payload["revision"]["source"] == "user_statement"
        assert created_payload["revision"]["lifecycle"] == "confirmed"
        assert created_payload["revision"]["belief_strength"] == "strong"
        assert created_payload["revision"]["claim"] is None
        assert created_payload["source_note"]["status"] == "retired"
        assert created_payload["source_note"]["text"] == statement
        assert created.headers["cache-control"] == "private, no-store"

        active_notes = client.get("/v1/context-notes", headers=OWNER_HEADERS)
        active_pages = client.get("/v1/knowledge", headers=OWNER_HEADERS)
        detail = client.get(f"/v1/knowledge/{page_id}", headers=OWNER_HEADERS)
        foreign_detail = client.get(
            f"/v1/knowledge/{page_id}",
            headers=FOREIGN_HEADERS,
        )
        assert active_notes.json() == []
        assert [page["id"] for page in active_pages.json()] == [page_id]
        assert detail.json()["revisions"][0]["statement"] == statement
        assert foreign_detail.status_code == 404

        account_id = owner.json()["id"]

        async def agent_read() -> tuple[dict, dict]:
            service = KnowledgeToolsService(repository=repository)
            context = StateContext({ACCOUNT_ID_STATE_KEY: account_id})
            index = await service.list_household_knowledge(context=context)
            selected = await service.read_household_knowledge_page(
                page_id=page_id,
                context=context,
            )
            return index.model_dump(mode="json"), selected.model_dump(mode="json")

        agent_index, agent_page = asyncio.run(agent_read())
        assert [page["page_id"] for page in agent_index["pages"]] == [page_id]
        assert agent_page["page"]["statement"] == statement
        assert agent_page["page"]["revision"]["source"] == "user_statement"

        corrected_statement = (
            "Meat placed in the air-fryer basket by the sink is usually steak, "
            "except when chicken was recently bought."
        )
        corrected = client.post(
            f"/v1/knowledge/{page_id}/correct",
            headers={**OWNER_HEADERS, "Idempotency-Key": "stable-teaching-correct-0001"},
            json={
                "statement": corrected_statement,
                "expected_revision_number": 1,
            },
        )
        corrected_retry = client.post(
            f"/v1/knowledge/{page_id}/correct",
            headers={**OWNER_HEADERS, "Idempotency-Key": "stable-teaching-correct-0001"},
            json={
                "statement": corrected_statement,
                "expected_revision_number": 1,
            },
        )
        stale = client.post(
            f"/v1/knowledge/{page_id}/correct",
            headers={**OWNER_HEADERS, "Idempotency-Key": "stable-teaching-correct-stale"},
            json={"statement": "A stale correction.", "expected_revision_number": 1},
        )
        assert corrected.status_code == corrected_retry.status_code == 200
        assert corrected_retry.json() == corrected.json()
        assert corrected.json()["revision"]["number"] == 2
        assert corrected.json()["revision"]["statement"] == corrected_statement
        assert corrected.json()["source_note"]["status"] == "retired"
        evidence = corrected.json()["revision"]["evidence"]
        assert {item["kind"] for item in evidence} == {
            "user_context_note",
            "knowledge_revision",
        }
        assert stale.status_code == 409
        assert stale.json() == {"detail": "knowledge_revision_changed"}

        retired = client.post(
            f"/v1/knowledge/{page_id}/retire",
            headers={**OWNER_HEADERS, "Idempotency-Key": "stable-teaching-retire-0001"},
            json={
                "expected_revision_number": 2,
                "reason": "This is no longer a reliable household rule.",
            },
        )
        retired_retry = client.post(
            f"/v1/knowledge/{page_id}/retire",
            headers={**OWNER_HEADERS, "Idempotency-Key": "stable-teaching-retire-0001"},
            json={
                "expected_revision_number": 2,
                "reason": "This is no longer a reliable household rule.",
            },
        )
        assert retired.status_code == retired_retry.status_code == 200
        assert retired_retry.json() == retired.json()
        assert retired.json()["revision"]["number"] == 3
        assert retired.json()["revision"]["lifecycle"] == "retired"
        assert retired.json()["revision"]["reason"] == (
            "This is no longer a reliable household rule."
        )
        assert client.get("/v1/knowledge", headers=OWNER_HEADERS).json() == []
        retired_pages = client.get(
            "/v1/knowledge?include_retired=true",
            headers=OWNER_HEADERS,
        )
        assert [page["id"] for page in retired_pages.json()] == [page_id]
        history = client.get(f"/v1/knowledge/{page_id}", headers=OWNER_HEADERS)
        assert [revision["number"] for revision in history.json()["revisions"]] == [1, 2, 3]
        assert history.json()["page"]["lifecycle"] == KnowledgeLifecycle.RETIRED

        source_notes = client.get(
            "/v1/context-notes?include_inactive=true",
            headers=OWNER_HEADERS,
        )
        assert source_notes.status_code == 200
        assert all(
            note["status"] == UserContextNoteStatus.RETIRED for note in source_notes.json()
        )
        assert {note["text"] for note in source_notes.json()} >= {
            statement,
            corrected_statement,
            "A stale correction.",
        }

        async def retired_agent_checks() -> None:
            service = KnowledgeToolsService(repository=repository)
            context = StateContext({ACCOUNT_ID_STATE_KEY: account_id})
            index = await service.list_household_knowledge(context=context)
            assert index.pages == []
            with pytest.raises(KnowledgePageNotFound):
                await service.read_household_knowledge_page(
                    page_id=page_id,
                    context=context,
                )

        asyncio.run(retired_agent_checks())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_stable_teaching_saga_preserves_raw_revisions_and_retires_notes() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-teaching-contract-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        await repository.provision_account("firestore-teaching-owner")
        service = HouseholdTeachingService(repository)
        initial = await service.teach(
            owner_user_id="firestore-teaching-owner",
            request=StableKnowledgeTeachingCreate(
                statement="The sink-side air-fryer basket usually means steak."
            ),
            idempotency_key="firestore-teaching-create-0001",
        )
        corrected = await service.correct(
            owner_user_id="firestore-teaching-owner",
            page_id=initial.page.id,
            request=StableKnowledgeCorrectionCreate(
                statement=(
                    "The sink-side air-fryer basket usually means steak unless chicken "
                    "was recently bought."
                ),
                expected_revision_number=1,
            ),
            idempotency_key="firestore-teaching-correct-0001",
        )
        retired = await service.retire(
            owner_user_id="firestore-teaching-owner",
            page_id=initial.page.id,
            request=StableKnowledgeRetirementCreate(expected_revision_number=2),
            idempotency_key="firestore-teaching-retire-0001",
        )
        history = await service.page_history(
            owner_user_id="firestore-teaching-owner",
            page_id=initial.page.id,
        )
        notes = await repository.list_user_context_notes(
            "firestore-teaching-owner",
            include_inactive=True,
        )

        assert initial.source_note.status == UserContextNoteStatus.RETIRED
        assert corrected.source_note.status == UserContextNoteStatus.RETIRED
        assert retired.revision.lifecycle == KnowledgeLifecycle.RETIRED
        assert [revision.number for revision in history.revisions] == [1, 2, 3]
        assert [revision.statement for revision in history.revisions[:2]] == [
            initial.revision.statement,
            corrected.revision.statement,
        ]
        assert all(note.status == UserContextNoteStatus.RETIRED for note in notes)
        assert await repository.list_knowledge_pages_for_owner(
            "firestore-teaching-owner"
        ) == []
        assert [
            page.id
            for page in await repository.list_knowledge_pages_for_owner(
                "firestore-teaching-owner",
                include_retired=True,
            )
        ] == [initial.page.id]
        client.close()

    asyncio.run(scenario())
