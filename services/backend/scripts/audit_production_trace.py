from __future__ import annotations

import argparse
import asyncio
import json

from foodlog_backend.ai_traces import (
    AiTraceService,
    audit_application_visible_trace,
)
from foodlog_backend.errors import AiTraceNotFound
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.storage import GCSObjectStore


async def audit(args: argparse.Namespace) -> None:
    repository = FirestoreRepository(
        project_id=args.project,
        public_account_limit=25,
        trial_image_limit=200,
    )
    service = AiTraceService(
        repository=repository,
        object_store=GCSObjectStore(
            project_id=args.project,
            bucket_name=args.bucket,
        ),
    )
    record = await repository.ai_trace_for_account(
        account_id=args.account_id,
        trace_id=args.trace_id,
    )
    payload = await service.read(account_id=args.account_id, trace_id=args.trace_id)
    audit_result = audit_application_visible_trace(payload)
    cross_account_denied = None
    if args.foreign_account_id:
        try:
            await service.read(
                account_id=args.foreign_account_id,
                trace_id=args.trace_id,
            )
        except AiTraceNotFound:
            cross_account_denied = True
        else:
            raise RuntimeError("A foreign account could read the private AI trace")
    print(
        json.dumps(
            {
                "schema_version": payload["schema_version"],
                "trace_id": record.id,
                "root_trace_id": record.root_trace_id,
                "parent_trace_id": record.parent_trace_id,
                "event_id": record.event_id,
                "reservation_id": record.reservation_id,
                "object_key": record.object_key,
                "content_sha256": record.content_sha256,
                "compressed_size": record.compressed_size,
                "status": record.status,
                "model": record.model,
                "prompt_version": record.prompt_version,
                "total_tokens": record.total_tokens,
                "actual_dkk_micros": record.actual_dkk_micros,
                "latency_ms": record.latency_ms,
                "cross_account_denied": cross_account_denied,
                **audit_result,
            },
            indent=2,
            sort_keys=True,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify one private production AI trace without printing its contents."
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--trace-id", required=True)
    parser.add_argument(
        "--foreign-account-id",
        help="Optional different account that must not be able to read the trace.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(audit(parse_args()))
