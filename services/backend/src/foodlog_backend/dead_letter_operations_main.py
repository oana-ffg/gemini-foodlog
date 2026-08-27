from __future__ import annotations

import argparse
import asyncio
import re
from uuid import uuid4

from .dead_letter_operations import (
    MAX_DEAD_LETTER_MESSAGES,
    DeadLetterOperationsService,
    DeadLetterStream,
)
from .firestore_repository import FirestoreRepository
from .models import AuditPurpose

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def _bounded_identifier(value: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("identifier has an unsafe shape")
    return value


def _project_id(value: str) -> str:
    if not PROJECT_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("project ID has an unsafe shape")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly replay bounded FoodLog dead-letter work."
    )
    parser.add_argument("--project-id", required=True, type=_project_id)
    parser.add_argument(
        "--stream",
        required=True,
        type=DeadLetterStream,
        choices=list(DeadLetterStream),
    )
    parser.add_argument("--purpose", required=True, type=AuditPurpose, choices=list(AuditPurpose))
    parser.add_argument("--session-id", type=_bounded_identifier, default=str(uuid4()))
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect",
        help="Lease, validate, audit, and immediately release messages.",
    )
    inspect.add_argument(
        "--max-messages",
        type=int,
        choices=range(1, MAX_DEAD_LETTER_MESSAGES + 1),
        default=MAX_DEAD_LETTER_MESSAGES,
    )
    replay = commands.add_parser("replay", help="Republish and acknowledge one exact message.")
    replay.add_argument("--message-id", required=True, type=_bounded_identifier)
    replay.add_argument("--confirm-message-id", required=True, type=_bounded_identifier)
    resolve = commands.add_parser(
        "acknowledge-resolved-image",
        help="Acknowledge one exact image message only after its durable job is complete.",
    )
    resolve.add_argument("--message-id", required=True, type=_bounded_identifier)
    resolve.add_argument("--confirm-message-id", required=True, type=_bounded_identifier)
    resolve.add_argument("--confirm-capture-id", required=True, type=_bounded_identifier)
    return parser


async def run(args: argparse.Namespace) -> None:
    repository = FirestoreRepository(
        project_id=args.project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    service = DeadLetterOperationsService(
        project_id=args.project_id,
        repository=repository,
    )
    if args.command == "inspect":
        result = await service.inspect(
            stream=args.stream,
            purpose=args.purpose,
            session_id=args.session_id,
            max_messages=args.max_messages,
        )
    elif args.command == "replay":
        result = await service.replay(
            stream=args.stream,
            message_id=args.message_id,
            confirmed_message_id=args.confirm_message_id,
            purpose=args.purpose,
            session_id=args.session_id,
        )
    else:
        if args.stream != DeadLetterStream.IMAGE:
            raise ValueError("resolved acknowledgement currently requires the image stream")
        result = await service.acknowledge_resolved_image(
            message_id=args.message_id,
            confirmed_message_id=args.confirm_message_id,
            confirmed_capture_id=args.confirm_capture_id,
            purpose=args.purpose,
            session_id=args.session_id,
        )
    print(result.model_dump_json(indent=2))


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
