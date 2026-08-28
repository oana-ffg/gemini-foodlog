from __future__ import annotations

import argparse
import asyncio
import re
from uuid import UUID, uuid4

from .firestore_repository import FirestoreRepository
from .models import AccountCapacityAction, AccountCapacityReason

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def _identifier(value: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("identifier has an unsafe shape")
    return value


def _project_id(value: str) -> str:
    if not PROJECT_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("project ID has an unsafe shape")
    return value


def _operation_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("operation ID must be a UUID") from error
    return str(parsed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or explicitly change one public account's admission-capacity state. "
            "The default is a read-only dry run."
        )
    )
    parser.add_argument("--project-id", required=True, type=_project_id)
    parser.add_argument("--account-id", required=True, type=_identifier)
    parser.add_argument(
        "--action",
        required=True,
        type=AccountCapacityAction,
        choices=list(AccountCapacityAction),
    )
    parser.add_argument(
        "--reason",
        required=True,
        type=AccountCapacityReason,
        choices=list(AccountCapacityReason),
    )
    parser.add_argument("--operation-id", type=_operation_id, default=str(uuid4()))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the exact audited change after the preview succeeds.",
    )
    return parser


async def run(args: argparse.Namespace) -> None:
    repository = FirestoreRepository(
        project_id=args.project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    preview = await repository.inspect_public_account_capacity(account_id=args.account_id)
    if args.action == AccountCapacityAction.RECLAIM:
        if args.reason == AccountCapacityReason.OPERATOR_REVERSAL:
            raise ValueError("reclaim requires an abuse or missing-identity reason")
        if preview.account_status != "active":
            raise ValueError("only an active public account can be reclaimed")
    else:
        if args.reason != AccountCapacityReason.OPERATOR_REVERSAL:
            raise ValueError("restore requires operator_reversal")
        if preview.account_status != "capacity_reclaimed":
            raise ValueError("only a reclaimed public account can be restored")
        if preview.active_public_account_count >= preview.account_limit:
            raise ValueError("public capacity is full; restore would exceed the limit")
    if not args.apply:
        print(preview.model_dump_json(indent=2))
        print(f"dry_run=true action={args.action.value} operation_id={args.operation_id}")
        return
    result = await repository.change_public_account_capacity(
        account_id=args.account_id,
        action=args.action,
        reason=args.reason,
        operation_id=args.operation_id,
    )
    print(result.model_dump_json(indent=2))


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
