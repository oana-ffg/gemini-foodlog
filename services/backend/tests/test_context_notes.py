from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from foodlog_backend.app import create_app
from foodlog_backend.errors import CrossAccountAccess, IdempotencyConflict
from foodlog_backend.models import UserContextNoteCreate, UserContextNoteStatus, utc_now
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.settings import Settings


def test_context_note_window_requires_offset_and_forward_order() -> None:
    now = utc_now()
    with pytest.raises(ValidationError, match="UTC offset"):
        UserContextNoteCreate(text="Duck tomorrow", valid_from=now.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="must be after"):
        UserContextNoteCreate(text="Duck tomorrow", valid_from=now, valid_until=now)


def test_repository_preserves_raw_note_expiry_retirement_and_tenant_scope() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        owner = await repository.provision_account("context-owner")
        await repository.provision_account("context-foreign")
        now = utc_now()
        request = UserContextNoteCreate(
            text="My MIL brought duck; we intend to cook it tomorrow.",
            valid_from=now,
            valid_until=now + timedelta(days=2),
        )
        note = await repository.create_user_context_note(
            owner_user_id="context-owner",
            request=request,
            idempotency_key="context-note-create-0001",
        )
        retry = await repository.create_user_context_note(
            owner_user_id="context-owner",
            request=request,
            idempotency_key="context-note-create-0001",
        )
        assert retry == note
        assert note.account_id == owner.id
        assert note.author_user_id == "context-owner"
        assert note.text == request.text
        assert await repository.list_user_context_notes(
            "context-owner", active_at=now + timedelta(hours=1)
        ) == [note]
        assert (
            await repository.list_user_context_notes(
                "context-owner", active_at=now + timedelta(days=3)
            )
            == []
        )
        assert await repository.list_user_context_notes(
            "context-owner",
            include_inactive=True,
            active_at=now + timedelta(days=3),
        ) == [note]

        with pytest.raises(IdempotencyConflict):
            await repository.create_user_context_note(
                owner_user_id="context-owner",
                request=UserContextNoteCreate(text="A different statement"),
                idempotency_key="context-note-create-0001",
            )
        with pytest.raises(CrossAccountAccess):
            await repository.retire_user_context_note(
                owner_user_id="context-foreign",
                note_id=note.id,
            )

        retired = await repository.retire_user_context_note(
            owner_user_id="context-owner",
            note_id=note.id,
        )
        assert retired.status == UserContextNoteStatus.RETIRED
        assert retired.retired_at is not None
        assert (
            await repository.retire_user_context_note(
                owner_user_id="context-owner",
                note_id=note.id,
            )
            == retired
        )
        assert await repository.list_user_context_notes("context-owner") == []
        assert await repository.list_user_context_notes("context-owner", include_inactive=True) == [
            retired
        ]

    asyncio.run(scenario())


def test_context_note_api_create_list_expire_retire_and_isolate() -> None:
    now = utc_now()
    owner_headers = {"X-FoodLog-Local-User": "context-api-owner"}
    foreign_headers = {"X-FoodLog-Local-User": "context-api-foreign"}
    with TestClient(create_app(Settings(environment="test"))) as client:
        assert client.post("/v1/accounts", headers=owner_headers).status_code == 200
        assert client.post("/v1/accounts", headers=foreign_headers).status_code == 200
        active_request = {
            "text": "  My MIL brought duck; we intend to cook it tomorrow.  ",
            "valid_from": (now - timedelta(hours=1)).isoformat(),
            "valid_until": (now + timedelta(days=2)).isoformat(),
        }
        active = client.post(
            "/v1/context-notes",
            headers={**owner_headers, "Idempotency-Key": "context-api-active-0001"},
            json=active_request,
        )
        exact_retry = client.post(
            "/v1/context-notes",
            headers={**owner_headers, "Idempotency-Key": "context-api-active-0001"},
            json=active_request,
        )
        changed_retry = client.post(
            "/v1/context-notes",
            headers={**owner_headers, "Idempotency-Key": "context-api-active-0001"},
            json={"text": "Different context"},
        )
        future = client.post(
            "/v1/context-notes",
            headers={**owner_headers, "Idempotency-Key": "context-api-future-0001"},
            json={
                "text": "Guests arrive next month.",
                "valid_from": (now + timedelta(days=20)).isoformat(),
                "valid_until": (now + timedelta(days=25)).isoformat(),
            },
        )
        expired = client.post(
            "/v1/context-notes",
            headers={**owner_headers, "Idempotency-Key": "context-api-expired-0001"},
            json={
                "text": "This was only relevant yesterday.",
                "valid_from": (now - timedelta(days=2)).isoformat(),
                "valid_until": (now - timedelta(days=1)).isoformat(),
            },
        )
        active_list = client.get("/v1/context-notes", headers=owner_headers)
        all_notes = client.get(
            "/v1/context-notes?include_inactive=true",
            headers=owner_headers,
        )
        foreign_list = client.get("/v1/context-notes", headers=foreign_headers)
        foreign_retire = client.post(
            f"/v1/context-notes/{active.json()['id']}/retire",
            headers=foreign_headers,
        )
        retired = client.post(
            f"/v1/context-notes/{active.json()['id']}/retire",
            headers=owner_headers,
        )
        after_retire = client.get("/v1/context-notes", headers=owner_headers)
        unauthenticated = client.get("/v1/context-notes")

    assert active.status_code == exact_retry.status_code == 201
    assert active.json() == exact_retry.json()
    assert active.json()["text"] == "My MIL brought duck; we intend to cook it tomorrow."
    assert changed_retry.status_code == 409
    assert changed_retry.json() == {"detail": "idempotency_key_reused_with_different_payload"}
    assert future.status_code == expired.status_code == 201
    assert [item["id"] for item in active_list.json()] == [active.json()["id"]]
    assert {item["id"] for item in all_notes.json()} == {
        active.json()["id"],
        future.json()["id"],
        expired.json()["id"],
    }
    assert foreign_list.json() == []
    assert foreign_retire.status_code == 404
    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"
    assert retired.json()["retired_at"] is not None
    assert after_retire.json() == []
    assert unauthenticated.status_code == 401
