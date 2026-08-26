import asyncio
from datetime import timedelta
from hashlib import sha256
from typing import Any

import pytest
from google.genai import types

from foodlog_agent.event_evidence_tool import (
    ACCOUNT_ID_STATE_KEY,
    EVENT_EVIDENCE_SCHEMA_VERSION,
    EVENT_ID_STATE_KEY,
    build_event_evidence_tool,
)
from foodlog_backend.errors import ActivityEventNotFound
from foodlog_backend.grouping import GroupingPolicy
from foodlog_backend.models import CaptureEnvelopeV1, capture_grouping_job_id, utc_now
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.storage import InMemoryObjectStore


class RecordingArtifactContext:
    def __init__(self, state: dict[str, str]) -> None:
        self.state = state
        self.saved: list[tuple[str, types.Part, dict[str, Any] | None]] = []

    async def save_artifact(
        self,
        filename: str,
        artifact: types.Part,
        custom_metadata: dict[str, Any] | None = None,
    ) -> int:
        self.saved.append((filename, artifact, custom_metadata))
        return len(self.saved) - 1


async def _store_and_group(
    *,
    repository: InMemoryRepository,
    object_store: InMemoryObjectStore,
    account,
    camera,
    capture_id: str,
    captured_at,
    sequence_number: int,
    content: bytes,
):
    object_key = f"accounts/{account.id}/captures/{capture_id}.jpg"
    capture, _, created = await repository.reserve_capture(
        capture_id=capture_id,
        account=account,
        camera=camera,
        idempotency_key=f"idempotency-{capture_id}",
        content_type="image/jpeg",
        content_sha256=sha256(content).hexdigest(),
        object_key=object_key,
        metadata=CaptureEnvelopeV1(
            camera_id=camera.id,
            captured_at=captured_at,
            client_kind="browser",
            client_version="event-tool-test/1",
            sequence_id=f"sequence-{camera.id}",
            sequence_number=sequence_number,
            burst_id="event-tool-burst-0001",
            burst_frame_index=sequence_number,
            width=1280,
            height=720,
        ),
    )
    assert created is True
    assert await object_store.put(object_key, content, "image/jpeg") is True
    await repository.mark_stored(account_id=account.id, capture_id=capture.id)
    lease_id = f"lease-{capture.id}"
    claimed = await repository.claim_job(
        account_id=account.id,
        job_id=capture_grouping_job_id(capture.id),
        expected_subject_revision=1,
        lease_id=lease_id,
        lease_owner="event-tool-test",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    assert claimed is not None
    result = await repository.group_capture(
        account_id=account.id,
        capture_id=capture.id,
        lease_id=lease_id,
        lease_owner="event-tool-test",
        policy=GroupingPolicy(),
    )
    assert result is not None
    return result


def test_event_tool_uses_trusted_scope_and_returns_ordered_private_artifacts() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        object_store = InMemoryObjectStore()
        account = await repository.provision_account("event-tool-owner")
        camera_a = await repository.create_browser_camera(
            "event-tool-owner", "Sink", "event-tool-camera-instance-a"
        )
        camera_b = await repository.create_browser_camera(
            "event-tool-owner", "Stove", "event-tool-camera-instance-b"
        )
        started_at = utc_now()
        later = await _store_and_group(
            repository=repository,
            object_store=object_store,
            account=account,
            camera=camera_a,
            capture_id="event-tool-capture-later",
            captured_at=started_at + timedelta(seconds=2),
            sequence_number=1,
            content=b"later-image",
        )
        earlier = await _store_and_group(
            repository=repository,
            object_store=object_store,
            account=account,
            camera=camera_b,
            capture_id="event-tool-capture-earlier",
            captured_at=started_at,
            sequence_number=0,
            content=b"earlier-image",
        )
        assert earlier.event.id == later.event.id

        context = RecordingArtifactContext(
            {
                ACCOUNT_ID_STATE_KEY: account.id,
                EVENT_ID_STATE_KEY: later.event.id,
            }
        )
        tool = build_event_evidence_tool(repository=repository, object_store=object_store)
        response = await tool.run_async(args={}, tool_context=context)  # type: ignore[arg-type]

        assert response["schema_version"] == EVENT_EVIDENCE_SCHEMA_VERSION
        assert response["event"]["event_id"] == later.event.id
        assert response["event"]["camera_ids"] == [camera_a.id, camera_b.id]
        assert [image["capture_id"] for image in response["ordered_images"]] == [
            "event-tool-capture-earlier",
            "event-tool-capture-later",
        ]
        assert [image["position"] for image in response["ordered_images"]] == [0, 1]
        assert [image["camera_id"] for image in response["ordered_images"]] == [
            camera_b.id,
            camera_a.id,
        ]
        assert response["ordered_images"][0]["captured_at"] == started_at.isoformat()
        assert response["ordered_images"][0]["sequence_number"] == 0
        assert response["ordered_images"][1]["sequence_number"] == 1
        assert "object_key" not in response["ordered_images"][0]
        assert "account_id" not in response
        assert [saved[1].inline_data.data for saved in context.saved] == [
            b"earlier-image",
            b"later-image",
        ]
        assert [saved[2]["capture_id"] for saved in context.saved if saved[2]] == [
            "event-tool-capture-earlier",
            "event-tool-capture-later",
        ]

    asyncio.run(scenario())


def test_event_tool_declaration_exposes_no_model_controlled_scope() -> None:
    tool = build_event_evidence_tool(
        repository=InMemoryRepository(public_account_limit=25, trial_image_limit=200),
        object_store=InMemoryObjectStore(),
    )

    declaration = tool._get_declaration().model_dump(mode="json", exclude_none=True)

    assert declaration["name"] == "get_current_event_evidence"
    assert "parameters" not in declaration
    assert "parameters_json_schema" not in declaration


def test_event_tool_cannot_read_another_accounts_event() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        object_store = InMemoryObjectStore()
        account_a = await repository.provision_account("event-tool-owner-a")
        account_b = await repository.provision_account("event-tool-owner-b")
        camera_a = await repository.create_browser_camera(
            "event-tool-owner-a", "Kitchen A", "event-tool-camera-instance-a"
        )
        result = await _store_and_group(
            repository=repository,
            object_store=object_store,
            account=account_a,
            camera=camera_a,
            capture_id="event-tool-private-capture",
            captured_at=utc_now(),
            sequence_number=0,
            content=b"private-image",
        )
        context = RecordingArtifactContext(
            {
                ACCOUNT_ID_STATE_KEY: account_b.id,
                EVENT_ID_STATE_KEY: result.event.id,
            }
        )
        tool = build_event_evidence_tool(repository=repository, object_store=object_store)

        with pytest.raises(ActivityEventNotFound):
            await tool.run_async(args={}, tool_context=context)  # type: ignore[arg-type]
        assert context.saved == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "state",
    [
        {},
        {ACCOUNT_ID_STATE_KEY: "account-a"},
        {EVENT_ID_STATE_KEY: "event-a"},
        {ACCOUNT_ID_STATE_KEY: " account-a", EVENT_ID_STATE_KEY: "event-a"},
    ],
)
def test_event_tool_fails_closed_without_valid_trusted_scope(state: dict[str, str]) -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        context = RecordingArtifactContext(state)
        tool = build_event_evidence_tool(
            repository=repository,
            object_store=InMemoryObjectStore(),
        )

        with pytest.raises(ValueError, match="session state"):
            await tool.run_async(args={}, tool_context=context)  # type: ignore[arg-type]
        assert context.saved == []

    asyncio.run(scenario())
