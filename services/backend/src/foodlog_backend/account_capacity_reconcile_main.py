from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from firebase_admin import auth as firebase_auth
from google.cloud import firestore
from google.cloud.firestore_v1.async_client import AsyncClient

from .auth import firebase_app_for_project, normalize_verified_email
from .firestore_repository import public_capacity_state_problems, public_capacity_values

PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


@dataclass(frozen=True)
class EmailBackfill:
    firebase_uid: str
    email_normalized: str


async def _verified_email_for_uid(*, firebase_uid: str, firebase_app: Any) -> str:
    try:
        user = await asyncio.to_thread(firebase_auth.get_user, firebase_uid, firebase_app)
    except firebase_auth.UserNotFoundError as error:
        raise ValueError("Firebase identity disappeared during reconciliation") from error
    email_normalized = normalize_verified_email(user.email)
    if not user.email_verified or email_normalized is None:
        raise ValueError("Firebase identity lost its verified email during reconciliation")
    return email_normalized


def _project_id(value: str) -> str:
    if not PROJECT_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("project ID has an unsafe shape")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile public FoodLog accounts with Firebase identities. The default is "
            "read-only; missing identities are reported and never reclaimed automatically."
        )
    )
    parser.add_argument("--project-id", required=True, type=_project_id)
    parser.add_argument(
        "--apply-email-backfill",
        action="store_true",
        help="Backfill only exact verified-email evidence after a clean reconciliation.",
    )
    return parser


async def reconcile(
    *,
    client: AsyncClient,
    firebase_app: Any,
    configured_limit: int,
) -> tuple[dict[str, object], list[EmailBackfill]]:
    identities = [snapshot async for snapshot in client.collection("identities").stream()]
    accounts = [snapshot async for snapshot in client.collection("accounts").stream()]
    capacity = await client.collection("system").document("public_capacity").get()
    capacity_problems = public_capacity_state_problems(
        capacity,
        configured_limit=configured_limit,
    )
    if capacity.exists and not capacity_problems:
        counter_count, account_limit = public_capacity_values(
            capacity,
            configured_limit=configured_limit,
        )
    else:
        capacity_data = capacity.to_dict() or {}
        raw_count = capacity_data.get("active_account_count")
        raw_limit = capacity_data.get("account_limit")
        counter_count = (
            raw_count
            if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0
            else 0
        )
        account_limit = (
            raw_limit
            if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) and raw_limit >= 1
            else configured_limit
        )
    active_public_count = 0
    public_identity_count = 0
    backfills: list[EmailBackfill] = []
    missing_firebase_uids: list[str] = []
    problems = [f"capacity_state:{problem}" for problem in capacity_problems]
    verified_email_owners: dict[str, str] = {}
    public_account_owners: dict[str, str] = {}
    if not capacity.exists:
        problems.append("capacity_document_missing")

    for identity in identities:
        if identity.get("account_class") != "public":
            continue
        public_identity_count += 1
        firebase_uid = identity.id
        account_id = identity.get("account_id")
        status = identity.get("status")
        if (
            not isinstance(account_id, str)
            or not account_id
            or status not in {"active", "capacity_reclaimed"}
        ):
            problems.append(f"identity_state:{firebase_uid}")
            continue
        previous_identity = public_account_owners.setdefault(account_id, firebase_uid)
        if previous_identity != firebase_uid:
            problems.append(f"duplicate_account_binding:{account_id}")
            continue
        account, entitlement = await asyncio.gather(
            client.collection("accounts").document(account_id).get(),
            client.collection("accounts")
            .document(account_id)
            .collection("entitlements")
            .document("current")
            .get(),
        )
        if not account.exists or account.get("owner_user_id") != firebase_uid:
            problems.append(f"account_binding:{firebase_uid}")
            continue
        if (
            account.get("status") != status
            or not entitlement.exists
            or entitlement.get("entitlement_mode") != "trial"
        ):
            problems.append(f"account_binding:{firebase_uid}")
            continue
        if status == "active":
            active_public_count += 1
        try:
            user = await asyncio.to_thread(firebase_auth.get_user, firebase_uid, firebase_app)
        except firebase_auth.UserNotFoundError:
            missing_firebase_uids.append(firebase_uid)
            continue
        email_normalized = normalize_verified_email(user.email)
        if not user.email_verified or email_normalized is None:
            problems.append(f"firebase_email_unverified:{firebase_uid}")
            continue
        previous_owner = verified_email_owners.setdefault(email_normalized, firebase_uid)
        if previous_owner != firebase_uid:
            problems.append(
                f"duplicate_verified_email:{sha256(email_normalized.encode()).hexdigest()}"
            )
            continue
        stored_email = identity.get("admission_email_normalized")
        stored_verified = identity.get("admission_email_verified")
        if stored_email is None and stored_verified in {None, False}:
            backfills.append(
                EmailBackfill(
                    firebase_uid=firebase_uid,
                    email_normalized=email_normalized,
                )
            )
        elif not isinstance(stored_email, str) or not stored_email or stored_verified is not True:
            problems.append(f"admission_email_invalid:{firebase_uid}")

    for account in accounts:
        entitlement = await (
            account.reference.collection("entitlements").document("current").get()
        )
        if (
            entitlement.exists
            and entitlement.get("entitlement_mode") == "trial"
            and account.id not in public_account_owners
        ):
            problems.append(f"orphan_public_account:{account.id}")

    firebase_user_ids = await asyncio.to_thread(
        lambda: sorted(
            user.uid for user in firebase_auth.list_users(app=firebase_app).iterate_all()
        )
    )
    identity_ids = {identity.id for identity in identities}
    firebase_only_user_ids = sorted(set(firebase_user_ids) - identity_ids)

    if active_public_count != counter_count:
        problems.append(f"capacity_count:{active_public_count}:{counter_count}")
    if active_public_count > configured_limit:
        problems.append(f"active_account_limit_exceeded:{active_public_count}")
    report: dict[str, object] = {
        "schema_version": 1,
        "public_identity_count": public_identity_count,
        "active_public_account_count": active_public_count,
        "stored_active_account_count": counter_count,
        "account_limit": account_limit,
        "email_backfill_candidates": [
            {
                "firebase_uid": candidate.firebase_uid,
                "email_sha256": sha256(candidate.email_normalized.encode()).hexdigest(),
            }
            for candidate in backfills
        ],
        "missing_firebase_uids": sorted(missing_firebase_uids),
        "firebase_users_without_foodlog_identity": firebase_only_user_ids,
        "problems": sorted(problems),
    }
    return report, backfills


async def apply_email_backfill(
    *,
    client: AsyncClient,
    firebase_app: Any,
    candidate: EmailBackfill,
) -> None:
    current_email = await _verified_email_for_uid(
        firebase_uid=candidate.firebase_uid,
        firebase_app=firebase_app,
    )
    if current_email != candidate.email_normalized:
        raise ValueError("Firebase verified email changed after reconciliation")
    identity_ref = client.collection("identities").document(candidate.firebase_uid)
    transaction = client.transaction()

    @firestore.async_transactional
    async def apply(transaction):
        identity = await identity_ref.get(transaction=transaction)
        if (
            not identity.exists
            or identity.get("account_class") != "public"
            or identity.get("status") not in {"active", "capacity_reclaimed"}
        ):
            raise ValueError("identity changed after reconciliation")
        stored_email = identity.get("admission_email_normalized")
        stored_verified = identity.get("admission_email_verified")
        if stored_email == candidate.email_normalized and stored_verified is True:
            return
        if stored_email is not None or stored_verified not in {None, False}:
            raise ValueError("identity email evidence changed after reconciliation")
        transaction.update(
            identity_ref,
            {
                "admission_email_normalized": candidate.email_normalized,
                "admission_email_verified": True,
            },
        )

    await apply(transaction)


async def run(args: argparse.Namespace) -> None:
    client = AsyncClient(project=args.project_id)
    firebase_app = firebase_app_for_project(args.project_id)
    try:
        report, backfills = await reconcile(
            client=client,
            firebase_app=firebase_app,
            configured_limit=25,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        has_blocker = bool(report["problems"] or report["missing_firebase_uids"])
        if has_blocker:
            raise ValueError("reconciliation has report-and-stop findings")
        if not args.apply_email_backfill:
            print("dry_run=true")
            return
        for candidate in backfills:
            await apply_email_backfill(
                client=client,
                firebase_app=firebase_app,
                candidate=candidate,
            )
        final_report, remaining = await reconcile(
            client=client,
            firebase_app=firebase_app,
            configured_limit=25,
        )
        if remaining or final_report["problems"] or final_report["missing_firebase_uids"]:
            raise ValueError("post-backfill reconciliation did not converge")
        print(json.dumps(final_report, indent=2, sort_keys=True))
    finally:
        client.close()


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
