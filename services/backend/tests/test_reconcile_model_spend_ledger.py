from datetime import UTC, datetime

import pytest

from foodlog_backend.models import ModelSpendReservation, ModelUsageRecord
from scripts.reconcile_model_spend_ledger import reconciled_commitment


def reservation(identifier: str, *, amount: int) -> ModelSpendReservation:
    return ModelSpendReservation(
        id=identifier,
        account_id="model-ledger-account",
        event_id=f"event-{identifier}",
        reserved_dkk_micros=amount,
    )


def usage(source: ModelSpendReservation, *, actual: int) -> ModelUsageRecord:
    return ModelUsageRecord(
        id=source.id,
        reservation_id=source.id,
        account_id=source.account_id,
        event_id=source.event_id,
        model=source.model,
        region=source.region,
        purpose=source.purpose,
        prompt_version=source.prompt_version,
        retry_attempt=source.retry_attempt,
        evaluation=source.evaluation,
        outcome="succeeded",
        prompt_tokens=100,
        response_tokens=10,
        thinking_tokens=0,
        total_tokens=110,
        actual_usd_nanos=1,
        actual_dkk_micros=actual,
        reserved_dkk_micros=source.reserved_dkk_micros,
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def test_reconciliation_keeps_actual_spend_and_only_unsettled_reservations() -> None:
    settled = reservation("model-ledger-settled", amount=100)
    outstanding = reservation("model-ledger-outstanding", amount=75)

    committed, actual = reconciled_commitment(
        [settled, outstanding],
        [usage(settled, actual=3)],
    )

    assert committed == 78
    assert actual == 3


def test_reconciliation_rejects_usage_without_immutable_reservation() -> None:
    missing = reservation("model-ledger-missing", amount=100)

    with pytest.raises(RuntimeError, match="missing reservation"):
        reconciled_commitment([], [usage(missing, actual=3)])
