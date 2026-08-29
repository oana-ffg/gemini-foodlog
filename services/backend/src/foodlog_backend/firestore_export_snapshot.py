import math
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.field_path import FieldPath

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
    "capacity_operations",
    "cameras",
    "captures",
    "consents",
    "events",
    "feedback",
    "inbound_mail_addresses",
    "inbound_mail_usage",
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
    "raw_mail_processing",
    "segments",
    "traces",
    "user_context_notes",
)

# Firestore allows large account histories, but the public export worker has a
# fixed memory envelope. Bound metadata before retaining it; retained binary
# objects are streamed separately and do not count toward this JSON allowance.
MAX_EXPORT_JSON_FILE_BYTES = 10_000_000
MAX_EXPORT_JSON_TOTAL_BYTES = 64_000_000
MAX_EXPORT_COLLECTION_DOCUMENTS = 50_000
MAX_EXPORT_SOURCE_OBJECTS = 10_000

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

        json_files: list[ExportJsonFile] = []
        json_total_bytes = 0

        def add_json_file(path: str, payload: dict[str, object]) -> None:
            nonlocal json_total_bytes
            content = canonical_json(payload)
            if len(content) > MAX_EXPORT_JSON_FILE_BYTES:
                raise ValueError("account export JSON entry exceeds its size limit")
            if json_total_bytes + len(content) > MAX_EXPORT_JSON_TOTAL_BYTES:
                raise ValueError("account export JSON exceeds its aggregate size limit")
            json_files.append(ExportJsonFile(path=path, content=content))
            json_total_bytes += len(content)

        add_json_file(
            "data/account.json",
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
            },
        )

        identity_snapshot = await (
            self._client.collection("identities")
            .document(account_export.requested_by_user_id)
            .get(read_time=account_export.snapshot_at)
        )
        if (
            not identity_snapshot.exists
            or identity_snapshot.get("account_id") != account_export.account_id
        ):
            raise CrossAccountAccess
        waitlist_snapshot = await (
            self._client.collection("waitlist")
            .document(sha256(account_export.requested_by_user_id.encode()).hexdigest())
            .get(read_time=account_export.snapshot_at)
        )
        waitlist_data = (
            export_document(
                document_id=waitlist_snapshot.id,
                data=self._checked_data(
                    account_id=account_export.account_id,
                    snapshot=waitlist_snapshot,
                    require_account_id=False,
                ),
            )
            if waitlist_snapshot.exists
            else None
        )
        add_json_file(
            "data/admission.json",
            {
                "schema_version": 1,
                "identity": export_document(
                    document_id=identity_snapshot.id,
                    data=self._checked_data(
                        account_id=account_export.account_id,
                        snapshot=identity_snapshot,
                    ),
                ),
                "waitlist": waitlist_data,
            },
        )

        source_objects: list[ExportSourceObject] = []
        for collection in EXPORT_COLLECTIONS:
            documents: list[dict[str, object]] = []
            documents_bytes = 0
            revisions: list[dict[str, object]] | None = (
                [] if collection in {"knowledge", "meals"} else None
            )
            revisions_bytes = 0
            query = account_ref.collection(collection).order_by(
                FieldPath.document_id()
            )
            async for snapshot in query.stream(read_time=account_export.snapshot_at):
                if len(documents) >= MAX_EXPORT_COLLECTION_DOCUMENTS:
                    raise ValueError("account export collection document limit exceeded")
                data = self._checked_data(
                    account_id=account_export.account_id,
                    snapshot=snapshot,
                )
                document = export_document(document_id=snapshot.id, data=data)
                documents_bytes += len(canonical_json(document))
                if documents_bytes > MAX_EXPORT_JSON_FILE_BYTES:
                    raise ValueError("account export JSON entry exceeds its size limit")
                documents.append(document)

                retained = source_object(
                    account_id=account_export.account_id,
                    collection=collection,
                    document_id=snapshot.id,
                    data=data,
                )
                if retained is not None:
                    if len(source_objects) >= MAX_EXPORT_SOURCE_OBJECTS:
                        raise ValueError("account export source-object limit exceeded")
                    source_objects.append(retained)

                if revisions is not None:
                    revision_query = snapshot.reference.collection("revisions").order_by(
                        FieldPath.document_id()
                    )
                    async for revision_snapshot in revision_query.stream(
                        read_time=account_export.snapshot_at
                    ):
                        if len(revisions) >= MAX_EXPORT_COLLECTION_DOCUMENTS:
                            raise ValueError(
                                "account export revision document limit exceeded"
                            )
                        revision_data = self._checked_data(
                            account_id=account_export.account_id,
                            snapshot=revision_snapshot,
                        )
                        revision = {
                            "parent_document_id": snapshot.id,
                            **export_document(
                                document_id=revision_snapshot.id,
                                data=revision_data,
                            ),
                        }
                        revisions_bytes += len(canonical_json(revision))
                        if revisions_bytes > MAX_EXPORT_JSON_FILE_BYTES:
                            raise ValueError(
                                "account export revision JSON exceeds its size limit"
                            )
                        revisions.append(revision)

            add_json_file(
                f"data/{collection}.json",
                {
                    "schema_version": 1,
                    "collection": collection,
                    "documents": documents,
                },
            )
            if revisions is not None:
                add_json_file(
                    f"data/{collection}_revisions.json",
                    {
                        "schema_version": 1,
                        "collection": f"{collection}/revisions",
                        "documents": revisions,
                    },
                )

        return AccountExportSnapshot(
            account_id=account_export.account_id,
            export_id=account_export.id,
            snapshot_at=account_export.snapshot_at,
            json_files=tuple(json_files),
            source_objects=tuple(source_objects),
        )
