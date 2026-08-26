from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from foodlog_agent.event_processing import AdkEventReasoner, EventInferenceProcessor
from foodlog_agent.event_reasoning import (
    MAX_LLM_CALLS,
    _structured_response,
    _validate_source_identities,
    event_bundle,
)
from foodlog_agent.prompt import PROMPT_VERSION
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.repository import Repository

SMOKE_EVENT_ID = "adk-smoke-event-v1"

__all__ = [
    "MAX_LLM_CALLS",
    "_structured_response",
    "_validate_source_identities",
    "main",
    "run_smoke",
    "smoke_event_bundle",
]


@dataclass(frozen=True)
class AdkSmokeRecord:
    prompt_version: str
    invocation_id: str
    model_version: str | None
    prompt_tokens: int
    response_tokens: int
    thinking_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    account_id: str
    event_id: str
    region: str
    retry_attempt: int
    evaluation: bool
    reservation_id: str
    reserved_dkk_micros: int
    actual_dkk_micros: int
    response: dict[str, Any]


def smoke_event_bundle(
    *,
    event_id: str = SMOKE_EVENT_ID,
    capture_ids: list[str] | None = None,
) -> dict[str, Any]:
    return event_bundle(
        event_id=event_id,
        capture_ids=capture_ids or ["adk-smoke-capture-v1"],
    )


async def run_smoke(
    *,
    repository: Repository,
    invocation_key: str,
    account_id: str,
    event_id: str,
) -> AdkSmokeRecord:
    event, _ = await repository.event_evidence_for_account(
        account_id=account_id,
        event_id=event_id,
    )
    processor = EventInferenceProcessor(
        repository=repository,
        reasoner=AdkEventReasoner(),
        purpose="deployment_smoke",
        evaluation=True,
    )
    claimed = await processor.process(
        account_id=account_id,
        event_id=event_id,
        expected_revision=event.current_revision,
        worker_id="deployment-smoke",
        invocation_key=invocation_key,
    )
    if claimed is None:
        raise RuntimeError("Event inference job was not claimable for the deployment smoke")
    accounted = claimed.accounted
    if not await processor.release_evaluation(claimed):
        raise RuntimeError("Deployment smoke lost its event inference lease after success")
    inference = accounted.inference
    usage = accounted.usage

    return AdkSmokeRecord(
        prompt_version=PROMPT_VERSION,
        invocation_id=usage.invocation_id or "",
        model_version=usage.model_version,
        prompt_tokens=usage.prompt_tokens,
        response_tokens=usage.response_tokens,
        thinking_tokens=usage.thinking_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=usage.actual_usd_nanos / 1_000_000_000,
        account_id=usage.account_id,
        event_id=usage.event_id,
        region=usage.region,
        retry_attempt=usage.retry_attempt,
        evaluation=usage.evaluation,
        reservation_id=accounted.reservation.id,
        reserved_dkk_micros=accounted.reservation.reserved_dkk_micros,
        actual_dkk_micros=usage.actual_dkk_micros,
        response=inference.model_dump(mode="json"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one billable, deployment-only ADK inference smoke test."
    )
    parser.add_argument(
        "--confirm-billable-smoke",
        action="store_true",
        help="Confirm that exactly one bounded live model workflow is intended.",
    )
    parser.add_argument(
        "--invocation-key",
        required=True,
        help="Unique operator-supplied key for this one accounted invocation.",
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--event-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_billable_smoke:
        _parser().error("--confirm-billable-smoke is required")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required")
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
        model_spend_limit_dkk_micros=int(
            os.environ.get("FOODLOG_MODEL_SPEND_LIMIT_DKK_MICROS", "400000000")
        ),
    )
    print(
        json.dumps(
            asdict(
                asyncio.run(
                    run_smoke(
                        repository=repository,
                        invocation_key=args.invocation_key,
                        account_id=args.account_id,
                        event_id=args.event_id,
                    )
                )
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
