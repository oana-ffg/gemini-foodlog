import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore

from .account_exports import (
    AccountExportSnapshot,
    ExportJsonFile,
    ExportSourceObject,
    canonical_json,
)
from .errors import CrossAccountAccess
from .models import AccountExport
from .storage import validate_account_object_key

EXPORT_COLLECTIONS = (
    "audit_events",
    "cameras",
    "captures",
    "consents",
    "events",
    "feedback",
    "inbound_mail_addresses",
    "knowledge",
    "meals",
    "model_usage",
    "purchase_charges",
    "purchase_documents",
    "purchase_items",
    "purchase_normalizations",
    "purchase_reconciliations",
    "purchases",
    "question_responses",
    "questions",
    "raw_mail",
    "raw_mail_authentication",
    "segments",
    "traces",
    "user_context_notes",
)

# These fields are operational secrets or implementation-only handles. Object keys
# are consumed separately by the worker and must never appear in exported JSON.
FORBIDDEN_EXPORT_FIELDS = frozenset(
    {
        "api_key",
        "client_instance_id_hash",
        "credential_hash",
        "idempotency_hash",
        "idempotency_key",
        "lease_expires_at",
        "lease_id",
        "lease_owner",
        "object_key",
        "password",
        "request_hash",
        "secret",
        "token",
    }
)


def _json_value(value: Any) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("account export contains a non-finite number")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("account export contains a naive timestamp")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("account export document keys must be strings")
            if key in FORBIDDEN_EXPORT_FIELDS:
                continue
            result[key] = _json_value(nested)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise ValueError(f"account export contains unsupported {type(value).__name__} data")


def export_document(*, document_id: str, data: Mapping[str, Any]) -> dict[str, object]:
    sanitized = _json_value(data)
    if not isinstance(sanitized, dict):
        raise ValueError("account export document must be an object")
    return {"document_id": document_id, **sanitized}


def _required_string(data: Mapping[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"account export source is missing {field}")
    return value


def _required_size(data: Mapping[str, Any], field: str) -> int:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"account export source has invalid {field}")
    return value


def source_object(
    *,
    account_id: str,
    collection: str,
    document_id: str,
    data: Mapping[str, Any],
) -> ExportSourceObject | None:
    if collection == "captures":
        content_type = _required_string(data, "content_type")
        extension = {"image/jpeg": "jpg", "image/png": "png"}.get(content_type)
        if extension is None:
            raise ValueError("account export capture has unsupported content type")
        source = "media"
        archive_path = f"media/{document_id}.{extension}"
        size = None
    elif collection == "raw_mail":
        source = "raw_mail"
        content_type = "message/rfc822"
        archive_path = f"mail/{document_id}.eml"
        size = _required_size(data, "size_bytes")
    elif collection == "traces":
        source = "traces"
        content_type = "application/gzip"
        archive_path = f"traces/{document_id}.json.gz"
        size = _required_size(data, "compressed_size")
    else:
        return None
    object_key = _required_string(data, "object_key")
    validate_account_object_key(account_id, object_key)
    return ExportSourceObject(
        archive_path=archive_path,
        source=source,
        object_key=object_key,
        content_type=content_type,
        content_sha256=_required_string(data, "content_sha256"),
        size=size,
    )


class FirestoreAccountExportSnapshotReader:
    """Reads one consistent, allowlisted account snapshot at the admitted read time."""

    def __init__(self, client: firestore.AsyncClient) -> None:
        self._client = client

    async def _documents(self, query: Any, *, read_time: datetime) -> list[Any]:
        return [snapshot async for snapshot in query.stream(read_time=read_time)]

    @staticmethod
    def _checked_data(
        *,
        account_id: str,
        snapshot: Any,
        require_account_id: bool = True,
    ) -> dict[str, Any]:
        data = snapshot.to_dict() or {}
        if not isinstance(data, dict):
            raise ValueError("account export Firestore document must be an object")
        if require_account_id and data.get("account_id") != account_id:
            raise CrossAccountAccess
        return data

    async def read(self, account_export: AccountExport) -> AccountExportSnapshot:
        account_ref = self._client.collection("accounts").document(account_export.account_id)
        account_snapshot = await account_ref.get(read_time=account_export.snapshot_at)
        if not account_snapshot.exists:
            raise ValueError("account export account snapshot does not exist")
        account_data = self._checked_data(
            account_id=account_export.account_id,
            snapshot=account_snapshot,
            require_account_id=False,
        )
        if account_data.get("owner_user_id") != account_export.requested_by_user_id:
            raise CrossAccountAccess
        entitlement_snapshot = await (
            account_ref.collection("entitlements")
            .document("current")
            .get(read_time=account_export.snapshot_at)
        )
        if not entitlement_snapshot.exists:
            raise ValueError("account export entitlement snapshot does not exist")
        entitlement_data = self._checked_data(
            account_id=account_export.account_id,
            snapshot=entitlement_snapshot,
            require_account_id=False,
        )
        json_files = [
            ExportJsonFile(
                path="data/account.json",
                content=canonical_json(
                    {
                        "schema_version": 1,
                        "account": export_document(
                            document_id=account_snapshot.id,
                            data=account_data,
                        ),
                        "entitlement": export_document(
                            document_id=entitlement_snapshot.id,
                            data=entitlement_data,
                        ),
                    }
                ),
            )
        ]
        source_objects: list[ExportSourceObject] = []
        collection_snapshots: dict[str, list[Any]] = {}
        for collection in EXPORT_COLLECTIONS:
            snapshots = await self._documents(
                account_ref.collection(collection),
                read_time=account_export.snapshot_at,
            )
            snapshots.sort(key=lambda snapshot: snapshot.id)
            collection_snapshots[collection] = snapshots
            documents: list[dict[str, object]] = []
            for snapshot in snapshots:
                data = self._checked_data(
                    account_id=account_export.account_id,
                    snapshot=snapshot,
                )
                documents.append(export_document(document_id=snapshot.id, data=data))
                retained = source_object(
                    account_id=account_export.account_id,
                    collection=collection,
                    document_id=snapshot.id,
                    data=data,
                )
                if retained is not None:
                    source_objects.append(retained)
            json_files.append(
                ExportJsonFile(
                    path=f"data/{collection}.json",
                    content=canonical_json(
                        {
                            "schema_version": 1,
                            "collection": collection,
                            "documents": documents,
                        }
                    ),
                )
            )

        for parent_collection in ("knowledge", "meals"):
            revisions: list[dict[str, object]] = []
            for parent in collection_snapshots[parent_collection]:
                revision_snapshots = await self._documents(
                    parent.reference.collection("revisions"),
                    read_time=account_export.snapshot_at,
                )
                revision_snapshots.sort(key=lambda snapshot: snapshot.id)
                for snapshot in revision_snapshots:
                    data = self._checked_data(
                        account_id=account_export.account_id,
                        snapshot=snapshot,
                    )
                    revisions.append(
                        {
                            "parent_document_id": parent.id,
                            **export_document(document_id=snapshot.id, data=data),
                        }
                    )
            json_files.append(
                ExportJsonFile(
                    path=f"data/{parent_collection}_revisions.json",
                    content=canonical_json(
                        {
                            "schema_version": 1,
                            "collection": f"{parent_collection}/revisions",
                            "documents": revisions,
                        }
                    ),
                )
            )

        return AccountExportSnapshot(
            account_id=account_export.account_id,
            export_id=account_export.id,
            snapshot_at=account_export.snapshot_at,
            json_files=tuple(json_files),
            source_objects=tuple(source_objects),
        )
