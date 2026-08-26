from __future__ import annotations

import os
from functools import lru_cache
from hashlib import sha256
from typing import Any, Protocol

from google.adk.tools import FunctionTool, ToolContext
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    ActivityEvent,
    CaptureRecord,
    CaptureStatus,
    MotionMetadataV1,
)
from foodlog_backend.repository import Repository
from foodlog_backend.storage import GCSObjectStore, ObjectStore

ACCOUNT_ID_STATE_KEY = "account_id"
EVENT_ID_STATE_KEY = "current_event_id"
EVENT_REVISION_STATE_KEY = "current_event_revision"
EVENT_EVIDENCE_SCHEMA_VERSION = "event-evidence-v1"


class ArtifactContext(Protocol):
    state: Any

    async def save_artifact(
        self,
        filename: str,
        artifact: types.Part,
        custom_metadata: dict[str, Any] | None = None,
    ) -> int: ...


class EventEvidenceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    status: str
    current_revision: int = Field(ge=1)
    camera_ids: list[str]
    first_capture_at: str
    last_capture_at: str
    capture_count: int = Field(ge=1)
    grouping_policy_version: str


class OrderedImageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=0)
    capture_id: str
    camera_id: str
    captured_at: str
    received_at: str
    status: CaptureStatus
    content_type: str
    content_sha256: str
    artifact_name: str
    artifact_version: int = Field(ge=0)
    client_kind: str | None = None
    client_version: str | None = None
    sequence_id: str | None = None
    sequence_number: int | None = Field(default=None, ge=0)
    burst_id: str | None = None
    burst_frame_index: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    motion: MotionMetadataV1 | None = None


class EventEvidenceToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = EVENT_EVIDENCE_SCHEMA_VERSION
    event: EventEvidenceSummary
    ordered_images: list[OrderedImageEvidence] = Field(min_length=1)


def _required_state_identifier(context: ArtifactContext, key: str) -> str:
    value = context.state.get(key)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 160
    ):
        raise ValueError(f"Agent session state is missing a valid {key}")
    return value


def _event_summary(event: ActivityEvent) -> EventEvidenceSummary:
    return EventEvidenceSummary(
        event_id=event.id,
        status=event.status.value,
        current_revision=event.current_revision,
        camera_ids=event.camera_ids,
        first_capture_at=event.first_capture_at.isoformat(),
        last_capture_at=event.last_capture_at.isoformat(),
        capture_count=event.capture_count,
        grouping_policy_version=event.grouping_policy_version,
    )


def _artifact_name(position: int, capture: CaptureRecord) -> str:
    extension = {"image/jpeg": "jpg", "image/png": "png"}.get(capture.content_type)
    if extension is None:
        raise ValueError("Activity event contains an unsupported image type")
    return f"event-image-{position:06d}-{capture.id}.{extension}"


class EventEvidenceToolService:
    def __init__(self, *, repository: Repository, object_store: ObjectStore) -> None:
        self._repository = repository
        self._object_store = object_store

    async def get_current_event_evidence(
        self,
        *,
        context: ArtifactContext,
    ) -> EventEvidenceToolResult:
        account_id = _required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        event_id = _required_state_identifier(context, EVENT_ID_STATE_KEY)
        event, captures = await self._repository.event_evidence_for_account(
            account_id=account_id,
            event_id=event_id,
        )
        expected_revision = context.state.get(EVENT_REVISION_STATE_KEY)
        if expected_revision is not None and (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise ValueError("Agent session state has an invalid current_event_revision")
        if expected_revision is not None and event.current_revision != expected_revision:
            raise ValueError("Activity event revision changed before evidence loading")

        expected_prefix = f"accounts/{account_id}/captures/"
        ordered_images: list[OrderedImageEvidence] = []
        for position, capture in enumerate(captures):
            if capture.account_id != account_id or not capture.object_key.startswith(
                expected_prefix
            ):
                raise ValueError("Activity event image escaped its account scope")
            content = await self._object_store.get(account_id, capture.object_key)
            if sha256(content).hexdigest() != capture.content_sha256:
                raise ValueError("Activity event image failed its integrity check")
            artifact_name = _artifact_name(position, capture)
            artifact_version = await context.save_artifact(
                artifact_name,
                types.Part.from_bytes(data=content, mime_type=capture.content_type),
                custom_metadata={
                    "schema_version": EVENT_EVIDENCE_SCHEMA_VERSION,
                    "event_id": event.id,
                    "capture_id": capture.id,
                    "camera_id": capture.camera_id,
                    "position": position,
                    "content_sha256": capture.content_sha256,
                },
            )
            metadata = capture.metadata
            ordered_images.append(
                OrderedImageEvidence(
                    position=position,
                    capture_id=capture.id,
                    camera_id=capture.camera_id,
                    captured_at=(
                        metadata.captured_at if metadata is not None else capture.created_at
                    ).isoformat(),
                    received_at=capture.created_at.isoformat(),
                    status=capture.status,
                    content_type=capture.content_type,
                    content_sha256=capture.content_sha256,
                    artifact_name=artifact_name,
                    artifact_version=artifact_version,
                    client_kind=metadata.client_kind if metadata is not None else None,
                    client_version=metadata.client_version if metadata is not None else None,
                    sequence_id=metadata.sequence_id if metadata is not None else None,
                    sequence_number=metadata.sequence_number if metadata is not None else None,
                    burst_id=metadata.burst_id if metadata is not None else None,
                    burst_frame_index=(
                        metadata.burst_frame_index if metadata is not None else None
                    ),
                    width=metadata.width if metadata is not None else None,
                    height=metadata.height if metadata is not None else None,
                    motion=metadata.motion if metadata is not None else None,
                )
            )

        return EventEvidenceToolResult(
            event=_event_summary(event),
            ordered_images=ordered_images,
        )


def build_event_evidence_tool(
    *,
    repository: Repository,
    object_store: ObjectStore,
) -> FunctionTool:
    service = EventEvidenceToolService(repository=repository, object_store=object_store)

    async def get_current_event_evidence(tool_context: ToolContext) -> dict[str, Any]:
        """Load the current account's current event and its ordered private images.

        Account and event scope come only from trusted session state. The result contains
        ordered camera/timing metadata and session-scoped artifact names; call load_artifacts
        with those names to inspect the image bytes.
        """
        result = await service.get_current_event_evidence(context=tool_context)
        return result.model_dump(mode="json")

    return FunctionTool(func=get_current_event_evidence)


@lru_cache(maxsize=1)
def production_event_evidence_service() -> EventEvidenceToolService:
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    bucket_name = os.environ.get("FOODLOG_MEDIA_BUCKET")
    if not project_id or not bucket_name:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT and FOODLOG_MEDIA_BUCKET are required for event evidence"
        )
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    object_store = GCSObjectStore(project_id=project_id, bucket_name=bucket_name)
    return EventEvidenceToolService(repository=repository, object_store=object_store)


async def get_current_event_evidence(tool_context: ToolContext) -> dict[str, Any]:
    """Load the current account's current event and its ordered private images.

    Account and event scope come only from trusted session state. The result contains
    ordered camera/timing metadata and session-scoped artifact names; call load_artifacts
    with those names to inspect the image bytes.
    """
    result = await production_event_evidence_service().get_current_event_evidence(
        context=tool_context
    )
    return result.model_dump(mode="json")


event_evidence_tool = FunctionTool(func=get_current_event_evidence)
