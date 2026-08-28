from __future__ import annotations

import argparse
import re
from dataclasses import asdict, dataclass, replace
from hashlib import sha256

from google.cloud import firestore, storage

from mail_gateway.config import quota_policy_from_environment
from mail_gateway.domain import RawMailUsage, utc_now, validate_raw_mail_object_key

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RETAINED_STATUSES = frozenset({"stored", "published"})
SUPPORTED_STATUSES = RETAINED_STATUSES | {"reserved"}
MAX_BACKFILL_ATTEMPTS = 3


class BackfillSnapshotChanged(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class RawMailLedgerRow:
    id: str
    status: str
    size_bytes: int
    object_key: str
    content_sha256: str


def _ledger_rows(*, account_id: str, snapshots) -> tuple[RawMailLedgerRow, ...]:
    rows: list[RawMailLedgerRow] = []
    for snapshot in snapshots:
        data = snapshot.to_dict() or {}
        status = data.get("status")
        size_bytes = data.get("size_bytes")
        object_key = data.get("object_key")
        content_sha256 = data.get("content_sha256")
        if (
            data.get("account_id") != account_id
            or status not in SUPPORTED_STATUSES
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 1
            or not isinstance(object_key, str)
            or not isinstance(content_sha256, str)
            or SHA256_PATTERN.fullmatch(content_sha256) is None
        ):
            raise ValueError(f"raw-mail metadata is invalid for account {account_id}")
        validate_raw_mail_object_key(
            account_id=account_id,
            mail_id=snapshot.id,
            object_key=object_key,
        )
        rows.append(
            RawMailLedgerRow(
                id=snapshot.id,
                status=status,
                size_bytes=size_bytes,
                object_key=object_key,
                content_sha256=content_sha256,
            )
        )
    return tuple(sorted(rows))


def _verify_account_objects(
    *,
    bucket,
    account_id: str,
    rows: tuple[RawMailLedgerRow, ...],
) -> None:
    expected = {row.object_key: row for row in rows}
    required = {row.object_key for row in rows if row.status in RETAINED_STATUSES}
    actual = {
        blob.name: blob for blob in bucket.list_blobs(prefix=f"accounts/{account_id}/raw-mail/")
    }
    missing = required - actual.keys()
    unexpected = actual.keys() - expected.keys()
    if missing or unexpected:
        raise ValueError(f"raw-mail object set differs from metadata for account {account_id}")
    for object_key, blob in actual.items():
        row = expected[object_key]
        content = blob.download_as_bytes()
        if len(content) != row.size_bytes or sha256(content).hexdigest() != row.content_sha256:
            raise ValueError(f"raw-mail object integrity failed for {row.id}")


def _verify_no_unknown_account_objects(*, bucket, account_ids: frozenset[str]) -> None:
    for blob in bucket.list_blobs(prefix="accounts/"):
        parts = blob.name.split("/")
        if (
            len(parts) != 4
            or parts[0] != "accounts"
            or parts[2] != "raw-mail"
            or parts[1] not in account_ids
        ):
            raise ValueError("raw-mail bucket contains an object outside a known account")


def _usage_for_rows(rows: tuple[RawMailLedgerRow, ...]) -> RawMailUsage:
    policy = quota_policy_from_environment()
    now = utc_now()
    usage = RawMailUsage.create(policy, now=now)
    retained = tuple(row for row in rows if row.status in RETAINED_STATUSES)
    pending = tuple(row for row in rows if row.status == "reserved")
    total_messages = len(retained) + len(pending)
    total_bytes = sum(row.size_bytes for row in retained) + sum(row.size_bytes for row in pending)
    if total_messages > policy.max_retained_messages or total_bytes > policy.max_retained_bytes:
        raise ValueError(
            "existing raw mail exceeds the configured hard cap; an explicit retention decision "
            "is required"
        )
    return replace(
        usage,
        retained_message_count=len(retained),
        retained_bytes=sum(row.size_bytes for row in retained),
        pending_message_count=len(pending),
        pending_bytes=sum(row.size_bytes for row in pending),
    )


def _validate_existing_usage(
    *,
    data: dict,
    account_id: str,
    expected: RawMailUsage,
) -> None:
    if data.get("schema_version") != 1 or data.get("account_id") != account_id:
        raise ValueError(f"inbound-mail usage identity is invalid for account {account_id}")
    usage_data = data.copy()
    usage_data.pop("schema_version")
    usage_data.pop("account_id")
    usage = RawMailUsage(**usage_data)
    for field_name in (
        "retained_message_count",
        "retained_bytes",
        "pending_message_count",
        "pending_bytes",
    ):
        if getattr(usage, field_name) != getattr(expected, field_name):
            raise ValueError(
                f"inbound-mail usage counters differ from metadata for account {account_id}"
            )
    if (
        usage.max_retained_messages > expected.max_retained_messages
        or usage.max_retained_bytes > expected.max_retained_bytes
        or usage.max_rate_messages > expected.max_rate_messages
        or usage.max_rate_bytes > expected.max_rate_bytes
        or usage.rate_window_seconds < expected.rate_window_seconds
    ):
        raise ValueError(
            f"inbound-mail usage ceilings are weaker than configuration for account {account_id}"
        )
    if (
        usage.retained_message_count + usage.pending_message_count > usage.max_retained_messages
        or usage.retained_bytes + usage.pending_bytes > usage.max_retained_bytes
    ):
        raise ValueError(
            f"existing raw mail exceeds the persisted hard cap for account {account_id}"
        )


def _backfill_account(*, client, bucket, account_id: str) -> bool:
    account_ref = client.collection("accounts").document(account_id)
    usage_ref = account_ref.collection("inbound_mail_usage").document("current")
    raw_mail = account_ref.collection("raw_mail")
    for _ in range(MAX_BACKFILL_ATTEMPTS):
        rows = _ledger_rows(account_id=account_id, snapshots=raw_mail.stream())
        _verify_account_objects(bucket=bucket, account_id=account_id, rows=rows)
        usage = _usage_for_rows(rows)
        transaction = client.transaction()

        @firestore.transactional
        def create_or_verify(
            transaction,
            expected_rows=rows,
            expected_usage=usage,
        ) -> bool:
            usage_snapshot = usage_ref.get(transaction=transaction)
            current_rows = _ledger_rows(
                account_id=account_id,
                snapshots=raw_mail.stream(transaction=transaction),
            )
            if current_rows != expected_rows:
                raise BackfillSnapshotChanged
            if usage_snapshot.exists:
                _validate_existing_usage(
                    data=usage_snapshot.to_dict() or {},
                    account_id=account_id,
                    expected=expected_usage,
                )
                return False
            transaction.create(
                usage_ref,
                {
                    "schema_version": 1,
                    "account_id": account_id,
                    **asdict(expected_usage),
                },
            )
            return True

        try:
            return create_or_verify(transaction)
        except BackfillSnapshotChanged:
            continue
    raise RuntimeError(
        f"raw mail kept changing during quota-ledger backfill for account {account_id}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill exact inbound-mail quota ledgers before gateway cutover."
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()
    client = firestore.Client(project=args.project)
    bucket = storage.Client(project=args.project).bucket(args.bucket)
    accounts = tuple(client.collection("accounts").stream())
    account_ids = frozenset(account.id for account in accounts)
    _verify_no_unknown_account_objects(bucket=bucket, account_ids=account_ids)
    created = 0
    existing = 0
    for account in accounts:
        if _backfill_account(client=client, bucket=bucket, account_id=account.id):
            created += 1
        else:
            existing += 1
    print(f"inbound-mail usage backfill complete: created={created} existing={existing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
