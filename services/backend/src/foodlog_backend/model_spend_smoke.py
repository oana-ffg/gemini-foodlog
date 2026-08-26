from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from .errors import ModelSpendLimitExceeded
from .firestore_repository import FirestoreRepository
from .models import ModelSpendReservation
from .repository import Repository

SMOKE_LEDGER_ID = "model_spend_smoke"
SMOKE_LIMIT_DKK_MICROS = 10_000
SMOKE_RESERVATION_ID = "model-spend-smoke-reservation-v1"
SMOKE_REJECTION_ID = "model-spend-smoke-rejection-v1"


async def run_smoke(
    *,
    repository: Repository,
    account_id: str,
    event_id: str,
) -> dict[str, Any]:
    accepted = await repository.reserve_model_spend(
        ModelSpendReservation(
            id=SMOKE_RESERVATION_ID,
            account_id=account_id,
            event_id=event_id,
            reserved_dkk_micros=SMOKE_LIMIT_DKK_MICROS,
        )
    )
    rejected = False
    try:
        await repository.reserve_model_spend(
            ModelSpendReservation(
                id=SMOKE_REJECTION_ID,
                account_id=account_id,
                event_id=event_id,
                reserved_dkk_micros=1,
            )
        )
    except ModelSpendLimitExceeded:
        rejected = True
    if not rejected:
        raise RuntimeError("model spend smoke failed to reject work above its ceiling")
    return {
        "schema_version": "model-spend-smoke-v1",
        "ledger_id": SMOKE_LEDGER_ID,
        "limit_dkk_micros": SMOKE_LIMIT_DKK_MICROS,
        "reserved_dkk_micros": accepted.reserved_dkk_micros,
        "overflow_dkk_micros": 1,
        "rejected_before_model_call": True,
        "model_calls": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the production Firestore model-spend hard stop without a model call."
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument(
        "--confirm-no-model-smoke",
        action="store_true",
        help="Confirm this execution may write the isolated model-spend smoke ledger.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_no_model_smoke:
        _parser().error("--confirm-no-model-smoke is required")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required")
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
        model_spend_limit_dkk_micros=SMOKE_LIMIT_DKK_MICROS,
        model_spend_ledger_id=SMOKE_LEDGER_ID,
    )
    print(
        json.dumps(
            asyncio.run(
                run_smoke(
                    repository=repository,
                    account_id=args.account_id,
                    event_id=args.event_id,
                )
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
