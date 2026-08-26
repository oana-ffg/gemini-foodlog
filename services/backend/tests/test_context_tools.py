import asyncio
from datetime import timedelta
from hashlib import sha256

import pytest

from foodlog_agent.context_tools import (
    CONTEXT_TOOL_RESULT_LIMIT,
    CONTEXT_TOOL_SCHEMA_VERSION,
    ContextToolsService,
    build_context_tools,
)
from foodlog_agent.context_tools_smoke import run_smoke
from foodlog_agent.event_evidence_tool import ACCOUNT_ID_STATE_KEY
from foodlog_backend.models import (
    CaptureEnvelopeV1,
    Confidence,
    MealComponent,
    MealEntry,
    MealStatus,
    UserContextNoteCreate,
    utc_now,
)
from foodlog_backend.repository import InMemoryRepository


class StateContext:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state


async def _save_meal(
    *,
    repository: InMemoryRepository,
    account,
    camera,
    sequence_number: int,
    status: MealStatus,
    occurred_at,
) -> MealEntry:
    capture_id = f"context-capture-{sequence_number}"
    content = f"context-image-{sequence_number}".encode()
    await repository.reserve_capture(
        capture_id=capture_id,
        account=account,
        camera=camera,
        idempotency_key=f"context-idempotency-{sequence_number}",
        content_type="image/jpeg",
        content_sha256=sha256(content).hexdigest(),
        object_key=f"accounts/{account.id}/captures/{capture_id}.jpg",
        metadata=CaptureEnvelopeV1(
            camera_id=camera.id,
            captured_at=occurred_at,
            client_kind="browser",
            client_version="context-tool-test/1",
            sequence_id="context-sequence",
            sequence_number=sequence_number,
            width=1280,
            height=720,
        ),
    )
    meal = MealEntry(
        id=f"context-meal-{sequence_number}",
        account_id=account.id,
        capture_id=capture_id,
        event_id=f"context-event-{sequence_number}",
        occurred_at=occurred_at,
        title=f"Meal {sequence_number}",
        confidence=Confidence.UNCERTAIN,
        components=[
            MealComponent(
                name="Main",
                ingredients=["ingredient"],
                preparation_methods=["unknown"],
            )
        ],
        observations=["Visible ingredient."],
        alternatives=[f"Alternative {sequence_number}"],
        rationale="Bounded test rationale.",
        status=status,
    )
    return await repository.save_meal(account_id=account.id, meal=meal)


def test_context_tools_are_bounded_active_provenanced_and_account_scoped() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("context-tool-owner")
        foreign = await repository.provision_account("context-tool-foreign")
        camera = await repository.create_browser_camera(
            "context-tool-owner", "Kitchen", "context-tool-camera"
        )
        now = utc_now()
        confirmed = await _save_meal(
            repository=repository,
            account=account,
            camera=camera,
            sequence_number=1,
            status=MealStatus.CONFIRMED,
            occurred_at=now - timedelta(hours=2),
        )
        provisional = await _save_meal(
            repository=repository,
            account=account,
            camera=camera,
            sequence_number=2,
            status=MealStatus.PROVISIONAL,
            occurred_at=now - timedelta(hours=1),
        )
        await _save_meal(
            repository=repository,
            account=account,
            camera=camera,
            sequence_number=3,
            status=MealStatus.NOT_COOKING,
            occurred_at=now,
        )
        for sequence_number in range(10, 31):
            await _save_meal(
                repository=repository,
                account=account,
                camera=camera,
                sequence_number=sequence_number,
                status=MealStatus.CONFIRMED,
                occurred_at=now - timedelta(days=sequence_number),
            )
        question = await repository.open_question(
            account_id=account.id,
            meal=provisional,
            prompt="Was the visible meat chicken or duck?",
            reason="The answer changes the ingredient identity.",
        )
        active = await repository.create_user_context_note(
            owner_user_id="context-tool-owner",
            request=UserContextNoteCreate(
                text="My MIL brought duck; we intend to cook it tonight.",
                valid_from=now - timedelta(hours=1),
                valid_until=now + timedelta(hours=8),
            ),
            idempotency_key="context-note-active",
        )
        await repository.create_user_context_note(
            owner_user_id="context-tool-owner",
            request=UserContextNoteCreate(
                text="Future note.",
                valid_from=now + timedelta(days=1),
                valid_until=now + timedelta(days=2),
            ),
            idempotency_key="context-note-future",
        )
        await repository.create_user_context_note(
            owner_user_id="context-tool-owner",
            request=UserContextNoteCreate(
                text="Expired note.",
                valid_until=now - timedelta(minutes=1),
            ),
            idempotency_key="context-note-expired",
        )
        retired = await repository.create_user_context_note(
            owner_user_id="context-tool-owner",
            request=UserContextNoteCreate(text="Retired note."),
            idempotency_key="context-note-retired",
        )
        await repository.retire_user_context_note(
            owner_user_id="context-tool-owner",
            note_id=retired.id,
        )

        tools = {tool.name: tool for tool in build_context_tools(repository=repository)}
        context = StateContext({ACCOUNT_ID_STATE_KEY: account.id})
        recent = await tools["get_recent_meals"].run_async(  # type: ignore[arg-type]
            args={}, tool_context=context
        )
        notes = await tools["get_active_user_context"].run_async(  # type: ignore[arg-type]
            args={}, tool_context=context
        )
        unresolved = await tools["get_unresolved_reviews"].run_async(  # type: ignore[arg-type]
            args={}, tool_context=context
        )

        assert recent["schema_version"] == CONTEXT_TOOL_SCHEMA_VERSION
        assert [meal["meal_id"] for meal in recent["meals"]][:2] == [
            provisional.id,
            confirmed.id,
        ]
        assert all(meal["status"] != "not_cooking" for meal in recent["meals"])
        assert notes["notes"] == [
            {
                "note_id": active.id,
                "text": active.text,
                "created_at": active.created_at.isoformat(),
                "valid_from": active.valid_from.isoformat(),
                "valid_until": active.valid_until.isoformat(),
            }
        ]
        assert [meal["meal_id"] for meal in unresolved["meals"]] == [
            provisional.id
        ]
        assert [item["question_id"] for item in unresolved["questions"]] == [
            question.id
        ]
        serialized = repr((recent, notes, unresolved))
        assert account.id not in serialized
        assert "account_id" not in serialized
        assert "author_user_id" not in serialized
        assert "object_key" not in serialized
        assert len(recent["meals"]) == CONTEXT_TOOL_RESULT_LIMIT

        smoke = await run_smoke(
            account_id=account.id,
            service=ContextToolsService(repository=repository),
        )
        assert smoke["recent_meal_count"] == CONTEXT_TOOL_RESULT_LIMIT
        assert smoke["active_note_ids"] == [active.id]
        assert smoke["unresolved_meal_ids"] == [provisional.id]
        assert smoke["open_question_ids"] == [question.id]
        assert smoke["model_calls"] == 0

        foreign_context = StateContext({ACCOUNT_ID_STATE_KEY: foreign.id})
        for tool in tools.values():
            response = await tool.run_async(  # type: ignore[arg-type]
                args={}, tool_context=foreign_context
            )
            assert not response.get("meals")
            assert not response.get("notes")
            assert not response.get("questions")

    asyncio.run(scenario())


def test_context_tool_declarations_expose_no_model_controlled_account_scope() -> None:
    tools = build_context_tools(
        repository=InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    )
    assert [tool.name for tool in tools] == [
        "get_recent_meals",
        "get_active_user_context",
        "get_unresolved_reviews",
    ]
    for tool in tools:
        declaration = tool._get_declaration().model_dump(mode="json", exclude_none=True)
        assert "parameters" not in declaration
        assert "parameters_json_schema" not in declaration


@pytest.mark.parametrize(
    "state",
    [{}, {ACCOUNT_ID_STATE_KEY: " account"}, {ACCOUNT_ID_STATE_KEY: ""}],
)
def test_context_tools_require_valid_trusted_account_state(
    state: dict[str, object],
) -> None:
    async def scenario() -> None:
        tools = build_context_tools(
            repository=InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        )
        with pytest.raises(ValueError, match="valid account_id"):
            await tools[0].run_async(  # type: ignore[arg-type]
                args={}, tool_context=StateContext(state)
            )

    asyncio.run(scenario())
