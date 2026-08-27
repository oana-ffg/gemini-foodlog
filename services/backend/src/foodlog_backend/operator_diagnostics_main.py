from __future__ import annotations

import argparse
import asyncio
import re
from uuid import uuid4

from .ai_traces import AiTraceService
from .firestore_repository import FirestoreRepository
from .models import AuditPurpose
from .operator_diagnostics import CloudLoggingDiagnosticReader, OperatorDiagnosticService
from .storage import GCSObjectStore

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


def _bounded_identifier(value: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("identifier has an unsafe shape")
    return value


def _project_id(value: str) -> str:
    if not PROJECT_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("project ID has an unsafe shape")
    return value


def _bucket_name(value: str) -> str:
    if not BUCKET_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("bucket name has an unsafe shape")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and inspect metadata for one exact FoodLog account event."
    )
    parser.add_argument("--project-id", required=True, type=_project_id)
    parser.add_argument("--media-bucket", required=True, type=_bucket_name)
    parser.add_argument("--trace-bucket", required=True, type=_bucket_name)
    parser.add_argument("--account-id", required=True, type=_bounded_identifier)
    parser.add_argument("--event-id", required=True, type=_bounded_identifier)
    parser.add_argument("--purpose", required=True, type=AuditPurpose, choices=list(AuditPurpose))
    parser.add_argument("--session-id", type=_bounded_identifier, default=str(uuid4()))
    return parser


async def run(args: argparse.Namespace) -> None:
    repository = FirestoreRepository(
        project_id=args.project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    media_store = GCSObjectStore(project_id=args.project_id, bucket_name=args.media_bucket)
    trace_store = GCSObjectStore(project_id=args.project_id, bucket_name=args.trace_bucket)
    service = OperatorDiagnosticService(
        repository=repository,
        media_store=media_store,
        trace_service=AiTraceService(repository=repository, object_store=trace_store),
        log_reader=CloudLoggingDiagnosticReader(project_id=args.project_id),
    )
    result = await service.inspect_event(
        account_id=args.account_id,
        event_id=args.event_id,
        purpose=args.purpose,
        session_id=args.session_id,
    )
    print(result.model_dump_json(indent=2))


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
