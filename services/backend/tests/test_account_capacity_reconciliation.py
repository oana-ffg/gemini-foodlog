import asyncio
from types import SimpleNamespace

import pytest
from firebase_admin import auth as firebase_auth

from foodlog_backend.account_capacity_reconcile_main import (
    EmailBackfill,
    _verified_email_for_uid,
    apply_email_backfill,
)


class FirestoreMustNotBeTouched:
    def collection(self, _name: str):
        raise AssertionError("a changed Firebase snapshot must stop before a Firestore write")


def test_email_backfill_stops_if_firebase_email_changes_between_scan_and_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = iter(
        [
            SimpleNamespace(email="first@example.test", email_verified=True),
            SimpleNamespace(email="second@example.test", email_verified=True),
        ]
    )
    monkeypatch.setattr(firebase_auth, "get_user", lambda _uid, _app: next(users))

    async def scenario() -> None:
        firebase_app = object()
        scanned_email = await _verified_email_for_uid(
            firebase_uid="changing-user",
            firebase_app=firebase_app,
        )
        candidate = EmailBackfill(
            firebase_uid="changing-user",
            email_normalized=scanned_email,
        )

        with pytest.raises(ValueError, match="changed after reconciliation"):
            await apply_email_backfill(
                client=FirestoreMustNotBeTouched(),  # type: ignore[arg-type]
                firebase_app=firebase_app,
                candidate=candidate,
            )

    asyncio.run(scenario())
