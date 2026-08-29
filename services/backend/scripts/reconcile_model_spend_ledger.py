from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

from google.cloud import firestore
from google.cloud.firestore import Client

from foodlog_backend.models import ModelSpendReservation, ModelUsageRecord


@dataclass(frozen=True)
class LedgerSnapshot:
    reserved_dkk_micros: int
    actual_dkk_micros: int
    reconciled_reservation_count: int
    updated_at: object


def _required_nonnegative_integer(data: dict[str, object], field: str) -> int:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"model spend ledger has invalid {field}")
    return value


def current_snapshot(data: dict[str, object]) -> LedgerSnapshot:
    return LedgerSnapshot(
        reserved_dkk_micros=_required_nonnegative_integer(data, "reserved_dkk_micros"),
        actual_dkk_micros=_required_nonnegative_integer(data, "actual_dkk_micros"),
        reconciled_reservation_count=_required_nonnegative_integer(
            data,
            "reconciled_reservation_count",
        ),
        updated_at=data.get("updated_at"),
    )


def reconciled_commitment(
    reservations: list[ModelSpendReservation],
    usage_records: list[ModelUsageRecord],
) -> tuple[int, int]:
    reservations_by_id = {reservation.id: reservation for reservation in reservations}
    if len(reservations_by_id) != len(reservations):
        raise RuntimeError("model spend reservations contain duplicate IDs")
    usage_by_id = {usage.reservation_id: usage for usage in usage_records}
    if len(usage_by_id) != len(usage_records):
        raise RuntimeError("model usage contains duplicate reservation IDs")
    for reservation_id, usage in usage_by_id.items():
        reservation = reservations_by_id.get(reservation_id)
        if reservation is None:
            raise RuntimeError("model usage references a missing reservation")
        if (
            usage.account_id != reservation.account_id
            or usage.event_id != reservation.event_id
            or usage.reserved_dkk_micros != reservation.reserved_dkk_micros
            or usage.actual_dkk_micros > reservation.reserved_dkk_micros
        ):
            raise RuntimeError("model usage conflicts with its immutable reservation")
    actual_total = sum(usage.actual_dkk_micros for usage in usage_records)
    outstanding_total = sum(
        reservation.reserved_dkk_micros
        for reservation in reservations
        if reservation.id not in usage_by_id
    )
    return actual_total + outstanding_total, actual_total


def reconcile(*, project_id: str, apply: bool) -> dict[str, int | bool]:
    client = Client(project=project_id)
    ledger_ref = client.collection("system").document("model_spend")
    try:
        ledger_document = ledger_ref.get()
        if not ledger_document.exists:
            raise RuntimeError("production model spend ledger does not exist")
        baseline = current_snapshot(ledger_document.to_dict() or {})
        reservations = [
            ModelSpendReservation.model_validate(snapshot.to_dict())
            for snapshot in ledger_ref.collection("reservations").stream()
        ]
        usage_records = [
            ModelUsageRecord.model_validate(snapshot.to_dict())
            for snapshot in client.collection_group("model_usage").stream()
        ]
        committed_total, actual_total = reconciled_commitment(reservations, usage_records)
        if actual_total != baseline.actual_dkk_micros:
            raise RuntimeError("immutable usage sum disagrees with the ledger actual total")
        if len(usage_records) != baseline.reconciled_reservation_count:
            raise RuntimeError("immutable usage count disagrees with the ledger reconciliation")

        if apply:
            transaction = client.transaction()

            @firestore.transactional
            def update(transaction):
                current_document = ledger_ref.get(transaction=transaction)
                current = current_snapshot(current_document.to_dict() or {})
                if current != baseline:
                    raise RuntimeError("model spend ledger changed during reconciliation")
                transaction.update(
                    ledger_ref,
                    {
                        "schema_version": 2,
                        "reservation_semantics": "actual_plus_unreconciled",
                        "reserved_dkk_micros": committed_total,
                        "updated_at": firestore.SERVER_TIMESTAMP,
                    },
                )

            update(transaction)

        return {
            "applied": apply,
            "reservation_count": len(reservations),
            "reconciled_reservation_count": len(usage_records),
            "previous_reserved_dkk_micros": baseline.reserved_dkk_micros,
            "reconciled_reserved_dkk_micros": committed_total,
            "actual_dkk_micros": actual_total,
        }
    finally:
        client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(reconcile(project_id=args.project, apply=args.apply), sort_keys=True))
