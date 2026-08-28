import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from firebase_admin import auth as firebase_auth

from foodlog_backend.account_capacity_reconcile_main import (
    EmailBackfill,
    _verified_email_for_uid,
    apply_email_backfill,
    reconcile,
)


class FirestoreMustNotBeTouched:
    def collection(self, _name: str):
        raise AssertionError("a changed Firebase snapshot must stop before a Firestore write")


class Snapshot:
    def __init__(self, *, document_id: str, data: dict[str, Any], reference=None) -> None:
        self.id = document_id
        self._data = data
        self.exists = True
        self.reference = reference

    def get(self, field: str) -> Any:
        return self._data[field]

    def to_dict(self) -> dict[str, Any]:
        return self._data.copy()


class DocumentReference:
    def __init__(self, client: "ReconcileClient", path: tuple[str, ...]) -> None:
        self._client = client
        self._path = path

    async def get(self) -> Snapshot:
        return self._client.snapshot(self._path)

    def collection(self, name: str) -> "CollectionReference":
        return CollectionReference(self._client, (*self._path, name))


class CollectionReference:
    def __init__(self, client: "ReconcileClient", path: tuple[str, ...]) -> None:
        self._client = client
        self._path = path

    def document(self, document_id: str) -> DocumentReference:
        return DocumentReference(self._client, (*self._path, document_id))

    async def stream(self):
        for path in sorted(self._client.documents):
            if len(path) == len(self._path) + 1 and path[:-1] == self._path:
                yield self._client.snapshot(path)


class ReconcileClient:
    def __init__(self) -> None:
        self.documents = {
            ("system", "public_capacity"): {
                "active_account_count": 1,
                "account_limit": 25,
                "waitlist_open": False,
            },
            ("identities", "legacy-user"): {
                "account_id": "legacy-account",
                "status": "active",
            },
            ("accounts", "legacy-account"): {
                "owner_user_id": "legacy-user",
                "status": "active",
            },
            ("accounts", "legacy-account", "entitlements", "current"): {
                "accepted_image_count": 0,
                "trial_image_limit": 200,
            },
        }

    def collection(self, name: str) -> CollectionReference:
        return CollectionReference(self, (name,))

    def snapshot(self, path: tuple[str, ...]) -> Snapshot:
        reference = DocumentReference(self, path)
        return Snapshot(document_id=path[-1], data=self.documents[path], reference=reference)


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


def test_reconcile_reports_exact_legacy_public_schema_backfill_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(
        uid="legacy-user",
        email="Legacy.User@Example.test",
        email_verified=True,
    )
    monkeypatch.setattr(firebase_auth, "get_user", lambda _uid, _app: user)
    monkeypatch.setattr(
        firebase_auth,
        "list_users",
        lambda app: SimpleNamespace(iterate_all=lambda: iter([user])),
    )

    async def scenario() -> None:
        report, email_backfills, legacy_backfills = await reconcile(
            client=ReconcileClient(),  # type: ignore[arg-type]
            firebase_app=object(),
            configured_limit=25,
        )

        assert report["problems"] == []
        assert report["active_public_account_count"] == 1
        assert email_backfills == []
        assert len(legacy_backfills) == 1
        assert legacy_backfills[0].firebase_uid == "legacy-user"
        assert legacy_backfills[0].account_id == "legacy-account"
        assert legacy_backfills[0].email_normalized == "legacy.user@example.test"

    asyncio.run(scenario())


def test_reconcile_treats_missing_identity_as_expected_after_capacity_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ReconcileClient()
    client.documents[("system", "public_capacity")]["active_account_count"] = 0
    identity = client.documents[("identities", "legacy-user")]
    identity.update(
        {
            "account_class": "public",
            "admission_email_normalized": None,
            "admission_email_verified": False,
            "status": "capacity_reclaimed",
        }
    )
    account = client.documents[("accounts", "legacy-account")]
    account.update({"entitlement_mode": "trial", "status": "capacity_reclaimed"})
    client.documents[("accounts", "legacy-account", "entitlements", "current")][
        "entitlement_mode"
    ] = "trial"

    def missing_user(_uid, _app):
        raise firebase_auth.UserNotFoundError("missing")

    monkeypatch.setattr(firebase_auth, "get_user", missing_user)
    monkeypatch.setattr(
        firebase_auth,
        "list_users",
        lambda app: SimpleNamespace(iterate_all=lambda: iter(())),
    )

    async def scenario() -> None:
        report, email_backfills, legacy_backfills = await reconcile(
            client=client,  # type: ignore[arg-type]
            firebase_app=object(),
            configured_limit=25,
        )

        assert report["problems"] == []
        assert report["active_public_account_count"] == 0
        assert report["missing_firebase_uids"] == []
        assert report["reclaimed_missing_firebase_uids"] == ["legacy-user"]
        assert email_backfills == []
        assert legacy_backfills == []

    asyncio.run(scenario())
