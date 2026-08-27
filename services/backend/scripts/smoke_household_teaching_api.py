from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from uuid import uuid4

import httpx

from foodlog_agent.event_evidence_tool import ACCOUNT_ID_STATE_KEY
from foodlog_agent.knowledge_tools import KnowledgeToolsService
from foodlog_backend.errors import KnowledgePageNotFound
from foodlog_backend.firestore_repository import FirestoreRepository

SYNTHETIC_MARKER = "Synthetic smoke-test rule:"


@dataclass
class StateContext:
    state: dict[str, object]


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected_status: int,
    headers: dict[str, str] | None = None,
    json: dict[str, object] | None = None,
) -> object:
    response = client.request(method, path, headers=headers, json=json)
    assert response.status_code == expected_status, (
        f"{method} {path}: expected {expected_status}, got {response.status_code}: "
        f"{response.text}"
    )
    return response.json()


async def verify_agent_tools(
    *,
    project_id: str,
    account_id: str,
    page_id: str,
    expected_statement: str,
) -> None:
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    service = KnowledgeToolsService(repository=repository)
    context = StateContext({ACCOUNT_ID_STATE_KEY: account_id})
    index = await service.list_household_knowledge(context=context)
    assert page_id in {page.page_id for page in index.pages}
    selected = await service.read_household_knowledge_page(
        page_id=page_id,
        context=context,
    )
    assert selected.page.statement == expected_statement
    assert selected.page.revision.source.value == "user_statement"


async def verify_retired_page_hidden_from_agent(
    *,
    project_id: str,
    account_id: str,
    page_id: str,
) -> None:
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    service = KnowledgeToolsService(repository=repository)
    context = StateContext({ACCOUNT_ID_STATE_KEY: account_id})
    index = await service.list_household_knowledge(context=context)
    assert page_id not in {page.page_id for page in index.pages}
    try:
        await service.read_household_knowledge_page(page_id=page_id, context=context)
    except KnowledgePageNotFound:
        pass
    else:
        raise AssertionError("retired page remained readable through agent tools")


def smoke(args: argparse.Namespace) -> None:
    email = os.environ["FOODLOG_SMOKE_EMAIL"]
    password = os.environ["FOODLOG_SMOKE_PASSWORD"]
    firebase_response = httpx.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
        params={"key": args.firebase_api_key},
        headers={
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=30,
    )
    firebase_response.raise_for_status()
    token = firebase_response.json()["idToken"]
    auth_headers = {"Authorization": f"Bearer {token}"}
    run_id = uuid4().hex
    initial_statement = (
        "Synthetic smoke-test rule: when the black air-fryer basket is beside the sink "
        f"and red meat is visible, the usual meal is steak ({run_id})."
    )
    corrected_statement = (
        "Synthetic smoke-test correction: the basket observation alone is not enough; "
        f"recent purchase evidence must also support steak ({run_id})."
    )
    create_key = f"teaching-smoke-create-{run_id}"
    correct_key = f"teaching-smoke-correct-{run_id}"
    retire_key = f"teaching-smoke-retire-{run_id}"

    with httpx.Client(
        base_url=args.api_url,
        headers={
            **auth_headers,
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        timeout=30,
    ) as client:
        account = request_json(client, "POST", "/v1/accounts", expected_status=200)
        assert isinstance(account, dict)
        account_id = account["id"]

        existing_pages = request_json(
            client, "GET", "/v1/knowledge", expected_status=200
        )
        assert isinstance(existing_pages, list)
        for existing_page in existing_pages:
            if not existing_page["title"].startswith(SYNTHETIC_MARKER):
                continue
            request_json(
                client,
                "POST",
                f"/v1/knowledge/{existing_page['id']}/retire",
                expected_status=200,
                headers={
                    "Idempotency-Key": f"teaching-smoke-recover-{existing_page['id']}"
                },
                json={
                    "expected_revision_number": existing_page["current_revision_number"],
                    "reason": "Recovered synthetic smoke-test cleanup; keep immutable history.",
                },
            )

        created = request_json(
            client,
            "POST",
            "/v1/knowledge",
            expected_status=201,
            headers={"Idempotency-Key": create_key},
            json={"statement": initial_statement},
        )
        retry = request_json(
            client,
            "POST",
            "/v1/knowledge",
            expected_status=201,
            headers={"Idempotency-Key": create_key},
            json={"statement": initial_statement},
        )
        conflict = request_json(
            client,
            "POST",
            "/v1/knowledge",
            expected_status=409,
            headers={"Idempotency-Key": create_key},
            json={"statement": f"Changed idempotency payload ({run_id})."},
        )
        assert retry == created
        assert conflict == {"detail": "idempotency_key_reused_with_different_payload"}
        assert isinstance(created, dict)
        page_id = created["page"]["id"]
        source_note_id = created["source_note"]["id"]
        assert created["source_note"]["status"] == "retired"
        assert created["source_note"]["text"] == initial_statement
        assert created["revision"]["statement"] == initial_statement
        assert created["revision"]["source"] == "user_statement"
        assert created["revision"]["lifecycle"] == "confirmed"
        assert created["revision"]["belief_strength"] == "strong"
        assert created["revision"]["claim"] is None

        active_notes = request_json(
            client, "GET", "/v1/context-notes", expected_status=200
        )
        all_notes = request_json(
            client,
            "GET",
            "/v1/context-notes?include_inactive=true",
            expected_status=200,
        )
        assert isinstance(active_notes, list) and isinstance(all_notes, list)
        assert source_note_id not in {note["id"] for note in active_notes}
        assert source_note_id in {
            note["id"] for note in all_notes if note["status"] == "retired"
        }
        active_pages = request_json(client, "GET", "/v1/knowledge", expected_status=200)
        detail = request_json(
            client, "GET", f"/v1/knowledge/{page_id}", expected_status=200
        )
        assert isinstance(active_pages, list) and isinstance(detail, dict)
        assert page_id in {page["id"] for page in active_pages}
        assert [revision["number"] for revision in detail["revisions"]] == [1]

        asyncio.run(
            verify_agent_tools(
                project_id=args.project,
                account_id=account_id,
                page_id=page_id,
                expected_statement=initial_statement,
            )
        )

        corrected = request_json(
            client,
            "POST",
            f"/v1/knowledge/{page_id}/correct",
            expected_status=200,
            headers={"Idempotency-Key": correct_key},
            json={
                "statement": corrected_statement,
                "expected_revision_number": 1,
            },
        )
        corrected_retry = request_json(
            client,
            "POST",
            f"/v1/knowledge/{page_id}/correct",
            expected_status=200,
            headers={"Idempotency-Key": correct_key},
            json={
                "statement": corrected_statement,
                "expected_revision_number": 1,
            },
        )
        assert corrected_retry == corrected
        assert isinstance(corrected, dict)
        assert corrected["revision"]["number"] == 2
        assert corrected["revision"]["statement"] == corrected_statement
        correction_note_id = corrected["source_note"]["id"]
        assert corrected["source_note"]["status"] == "retired"

        retired = request_json(
            client,
            "POST",
            f"/v1/knowledge/{page_id}/retire",
            expected_status=200,
            headers={"Idempotency-Key": retire_key},
            json={
                "expected_revision_number": 2,
                "reason": "Synthetic smoke-test cleanup; keep immutable history.",
            },
        )
        retired_retry = request_json(
            client,
            "POST",
            f"/v1/knowledge/{page_id}/retire",
            expected_status=200,
            headers={"Idempotency-Key": retire_key},
            json={
                "expected_revision_number": 2,
                "reason": "Synthetic smoke-test cleanup; keep immutable history.",
            },
        )
        assert retired_retry == retired
        assert isinstance(retired, dict)
        assert retired["revision"]["number"] == 3
        assert retired["revision"]["lifecycle"] == "retired"

        active_after_retire = request_json(
            client, "GET", "/v1/knowledge", expected_status=200
        )
        retired_pages = request_json(
            client,
            "GET",
            "/v1/knowledge?include_retired=true",
            expected_status=200,
        )
        history = request_json(
            client, "GET", f"/v1/knowledge/{page_id}", expected_status=200
        )
        notes_after = request_json(
            client,
            "GET",
            "/v1/context-notes?include_inactive=true",
            expected_status=200,
        )
        assert isinstance(active_after_retire, list)
        assert isinstance(retired_pages, list)
        assert isinstance(history, dict)
        assert isinstance(notes_after, list)
        assert page_id not in {page["id"] for page in active_after_retire}
        assert page_id in {page["id"] for page in retired_pages}
        assert [revision["number"] for revision in history["revisions"]] == [1, 2, 3]
        assert history["page"]["lifecycle"] == "retired"
        retired_note_ids = {
            note["id"] for note in notes_after if note["status"] == "retired"
        }
        assert {source_note_id, correction_note_id} <= retired_note_ids

    asyncio.run(
        verify_retired_page_hidden_from_agent(
            project_id=args.project,
            account_id=account_id,
            page_id=page_id,
        )
    )
    print(f"account_id={account_id}")
    print(f"page_id={page_id}")
    print("api_create_retry_conflict_verified=true")
    print("raw_source_notes_retired_verified=true")
    print("revision_history=1,2,3")
    print("owner_lists_and_agent_tools_verified=true")
    print("retired_page_hidden_from_agent=true")
    print("model_calls=0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--project", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
