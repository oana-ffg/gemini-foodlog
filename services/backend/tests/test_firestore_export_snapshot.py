import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

import foodlog_backend.firestore_export_snapshot as snapshot_module
from foodlog_backend.firestore_export_snapshot import (
    FirestoreAccountExportSnapshotReader,
)
from foodlog_backend.models import AccountExport


class FakeSnapshot:
    def __init__(self, reference: "FakeDocumentReference", data: dict[str, Any] | None):
        self.reference = reference
        self.id = reference.id
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None

    def get(self, field: str) -> Any:
        if self._data is None:
            raise KeyError(field)
        return self._data[field]


class FakeDocumentReference:
    def __init__(self, client: "FakeFirestoreClient", path: str):
        self._client = client
        self.path = path
        self.id = path.rsplit("/", 1)[-1]

    async def get(self, *, read_time: datetime) -> FakeSnapshot:
        assert read_time.tzinfo is not None
        return FakeSnapshot(self, self._client.documents.get(self.path))

    def collection(self, name: str) -> "FakeCollectionReference":
        return FakeCollectionReference(self._client, f"{self.path}/{name}")


class FakeCollectionReference:
    def __init__(self, client: "FakeFirestoreClient", path: str):
        self._client = client
        self.path = path
        self._ordered = False

    def document(self, document_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(self._client, f"{self.path}/{document_id}")

    def order_by(self, field: object) -> "FakeCollectionReference":
        assert field is not None
        self._ordered = True
        return self

    async def stream(self, *, read_time: datetime):  # type: ignore[no-untyped-def]
        assert self._ordered is True
        assert read_time.tzinfo is not None
        prefix = f"{self.path}/"
        direct_paths = [
            path
            for path in self._client.documents
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        ]
        for path in sorted(direct_paths):
            reference = FakeDocumentReference(self._client, path)
            yield FakeSnapshot(reference, self._client.documents[path])


class FakeFirestoreClient:
    def __init__(self, documents: dict[str, dict[str, Any]]):
        self.documents = documents

    def collection(self, name: str) -> FakeCollectionReference:
        return FakeCollectionReference(self, name)


def export_request() -> AccountExport:
    requested_at = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    return AccountExport(
        id="a" * 64,
        account_id="account-a",
        requested_by_user_id="owner-a",
        job_id="account-export-job",
        snapshot_at=requested_at,
        requested_at=requested_at,
    )


def export_documents() -> dict[str, dict[str, Any]]:
    base = {
        "accounts/account-a": {
            "owner_user_id": "owner-a",
            "status": "active",
        },
        "accounts/account-a/entitlements/current": {
            "plan": "trial",
        },
        "identities/owner-a": {
            "account_id": "account-a",
            "status": "active",
        },
    }
    for capture_id in ("capture-z", "capture-a"):
        base[f"accounts/account-a/captures/{capture_id}"] = {
            "account_id": "account-a",
            "content_type": "image/jpeg",
            "content_sha256": "b" * 64,
            "object_key": f"accounts/account-a/captures/{capture_id}.jpg",
        }
    return base


def test_firestore_snapshot_streams_collections_in_stable_order() -> None:
    async def scenario() -> None:
        reader = FirestoreAccountExportSnapshotReader(
            FakeFirestoreClient(export_documents())  # type: ignore[arg-type]
        )
        snapshot = await reader.read(export_request())
        captures_file = next(
            item for item in snapshot.json_files if item.path == "data/captures.json"
        )
        captures = json.loads(captures_file.content)["documents"]
        assert [document["document_id"] for document in captures] == [
            "capture-a",
            "capture-z",
        ]
        assert [item.archive_path for item in snapshot.source_objects] == [
            "media/capture-a.jpg",
            "media/capture-z.jpg",
        ]

    asyncio.run(scenario())


def test_firestore_snapshot_rejects_unbounded_collection_before_accumulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(snapshot_module, "MAX_EXPORT_COLLECTION_DOCUMENTS", 1)
        reader = FirestoreAccountExportSnapshotReader(
            FakeFirestoreClient(export_documents())  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="collection document limit"):
            await reader.read(export_request())

    asyncio.run(scenario())
