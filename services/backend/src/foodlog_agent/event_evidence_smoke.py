from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from google.genai import types

from foodlog_agent.event_evidence_tool import (
    ACCOUNT_ID_STATE_KEY,
    EVENT_ID_STATE_KEY,
    EventEvidenceToolResult,
    EventEvidenceToolService,
    production_event_evidence_service,
)


@dataclass(frozen=True)
class LiveArtifactRecord:
    filename: str
    mime_type: str
    size_bytes: int
    content_sha256: str
    metadata: dict[str, Any]


class VerifyingArtifactContext:
    """Exercise the tool's artifact boundary without persisting private smoke data."""

    def __init__(self, *, account_id: str, event_id: str) -> None:
        self.state = {
            ACCOUNT_ID_STATE_KEY: account_id,
            EVENT_ID_STATE_KEY: event_id,
        }
        self.artifacts: list[LiveArtifactRecord] = []

    async def save_artifact(
        self,
        filename: str,
        artifact: types.Part,
        custom_metadata: dict[str, Any] | None = None,
    ) -> int:
        if artifact.inline_data is None or artifact.inline_data.data is None:
            raise RuntimeError("event evidence tool produced a non-inline image artifact")
        content = artifact.inline_data.data
        self.artifacts.append(
            LiveArtifactRecord(
                filename=filename,
                mime_type=artifact.inline_data.mime_type,
                size_bytes=len(content),
                content_sha256=sha256(content).hexdigest(),
                metadata=dict(custom_metadata or {}),
            )
        )
        return len(self.artifacts) - 1


async def run_smoke(
    *,
    account_id: str,
    event_id: str,
    service: EventEvidenceToolService | None = None,
) -> dict[str, Any]:
    context = VerifyingArtifactContext(account_id=account_id, event_id=event_id)
    active_service = service or production_event_evidence_service()
    result = await active_service.get_current_event_evidence(context=context)
    EventEvidenceToolResult.model_validate(result)
    if len(context.artifacts) != result.event.capture_count:
        raise RuntimeError("event evidence smoke produced an incomplete artifact set")
    for image, artifact in zip(result.ordered_images, context.artifacts, strict=True):
        if image.content_sha256 != artifact.content_sha256:
            raise RuntimeError("event evidence artifact digest differs from tool metadata")
    return {
        "schema_version": result.schema_version,
        "event_id": result.event.event_id,
        "event_revision": result.event.current_revision,
        "camera_count": len(result.event.camera_ids),
        "ordered_capture_ids": [image.capture_id for image in result.ordered_images],
        "ordered_captured_at": [image.captured_at for image in result.ordered_images],
        "artifact_count": len(context.artifacts),
        "artifact_bytes": sum(artifact.size_bytes for artifact in context.artifacts),
        "artifact_records": [asdict(artifact) for artifact in context.artifacts],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read one private production event through the no-model evidence tool."
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument(
        "--confirm-private-read",
        action="store_true",
        help="Confirm this execution may read the selected private event images.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_private_read:
        _parser().error("--confirm-private-read is required")
    print(
        json.dumps(
            asyncio.run(run_smoke(account_id=args.account_id, event_id=args.event_id)),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
