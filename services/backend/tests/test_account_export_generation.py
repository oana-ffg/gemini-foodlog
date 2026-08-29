"""Account export archive generation and worker tests."""

import asyncio
import json
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from foodlog_backend.account_exports import (
    EXPORT_LEASE,
    EXPORT_PLATFORM_REQUEST_ENVELOPE,
    AccountExportService,
    AccountExportSnapshot,
    ExportJsonFile,
    ExportSourceObject,
    canonical_json,
    export_archive_object_key,
)
from foodlog_backend.errors import CrossAccountAccess
from foodlog_backend.export_worker_app import (
    ExportWorkerSettings,
    create_export_worker_app,
)
from foodlog_backend.firestore_export_snapshot import export_document, source_object
from foodlog_backend.models import AccountExport, AccountExportStatus
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.storage import InMemoryObjectStore


class StaticSnapshotReader:
    def __init__(self, snapshot: AccountExportSnapshot) -> None:
        self.snapshot = snapshot

    async def read(self, account_export: AccountExport) -> AccountExportSnapshot:
        assert account_export.account_id == self.snapshot.account_id
        assert account_export.id == self.snapshot.export_id
        return self.snapshot


class FailOnceSnapshotReader(StaticSnapshotReader):
    def __init__(self, snapshot: AccountExportSnapshot) -> None:
        super().__init__(snapshot)
        self.calls = 0

    async def read(self, account_export: AccountExport) -> AccountExportSnapshot:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated snapshot outage")
        return await super().read(account_export)


class CancellingSourceStore(InMemoryObjectStore):
    async def download_to_file(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError


class TrackingExportStore(InMemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.upload_started = False
        self.abort_called = False

    async def start_streaming_put(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.upload_started = True
        return await super().start_streaming_put(*args, **kwargs)

    async def abort_streaming_put(self, upload) -> None:  # type: ignore[no-untyped-def]
        self.abort_called = True
        await super().abort_streaming_put(upload)


def push_envelope(payload: dict, *, delivery_attempt: int = 1) -> dict:
    return {
        "message": {
            "data": b64encode(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).decode(),
            "messageId": "export-message-1",
        },
        "subscription": "projects/test/subscriptions/foodlog-export-consumer",
        "deliveryAttempt": delivery_attempt,
    }


async def prepared_export():
    repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    account = await repository.provision_account("export-owner")
    requested_at = datetime.now(UTC) - timedelta(seconds=1)
    account_export, created = await repository.create_account_export(
        owner_user_id="export-owner",
        idempotency_key="export-request-0001",
        requested_at=requested_at,
        cooldown=timedelta(hours=1),
    )
    assert created is True
    media = b"low-resolution-kitchen-image"
    raw_mail = b"From: shop@example.test\r\nSubject: Invoice\r\n\r\nOrder"
    trace = b"compressed-trace"
    stores = {
        "media": InMemoryObjectStore(),
        "raw_mail": InMemoryObjectStore(),
        "traces": InMemoryObjectStore(),
    }
    source_specs = (
        (
            "media",
            f"accounts/{account.id}/captures/capture-1.jpg",
            media,
            "image/jpeg",
            "media/capture-1.jpg",
        ),
        (
            "raw_mail",
            f"accounts/{account.id}/raw-mail/{'a' * 64}.eml",
            raw_mail,
            "message/rfc822",
            f"mail/{'a' * 64}.eml",
        ),
        (
            "traces",
            f"accounts/{account.id}/traces/trace-{'b' * 64}.json.gz",
            trace,
            "application/gzip",
            f"traces/trace-{'b' * 64}.json.gz",
        ),
    )
    retained = []
    for source, object_key, content, content_type, archive_path in source_specs:
        await stores[source].put(account.id, object_key, content, content_type)
        retained.append(
            ExportSourceObject(
                archive_path=archive_path,
                source=source,
                object_key=object_key,
                content_type=content_type,
                content_sha256=sha256(content).hexdigest(),
                size=len(content),
            )
        )
    snapshot = AccountExportSnapshot(
        account_id=account.id,
        export_id=account_export.id,
        snapshot_at=account_export.snapshot_at,
        json_files=(
            ExportJsonFile(
                path="data/account.json",
                content=canonical_json(
                    {
                        "schema_version": 1,
                        "account": {"id": account.id, "owner_user_id": "export-owner"},
                    }
                ),
            ),
        ),
        source_objects=tuple(retained),
    )
    return repository, account_export, stores, snapshot


def test_complete_account_export_contains_verified_manifest_and_retained_objects() -> None:
    async def scenario() -> None:
        repository, account_export, stores, snapshot = await prepared_export()
        export_store = InMemoryObjectStore()
        service = AccountExportService(
            repository=repository,
            snapshot_reader=StaticSnapshotReader(snapshot),
            source_stores=stores,
            export_store=export_store,
        )
        result = await service.process(
            account_id=account_export.account_id,
            export_id=account_export.id,
            worker_id="test-worker",
            delivery_attempt=1,
        )
        assert result is not None
        archive = await export_store.get(account_export.account_id, result.object_key)
        assert sha256(archive).hexdigest() == result.archive_sha256
        with ZipFile(BytesIO(archive)) as bundle:
            names = sorted(bundle.namelist())
            assert names == [
                "data/account.json",
                f"mail/{'a' * 64}.eml",
                "manifest.json",
                "media/capture-1.jpg",
                f"traces/trace-{'b' * 64}.json.gz",
            ]
            manifest_bytes = bundle.read("manifest.json")
            manifest = json.loads(manifest_bytes)
            assert sha256(manifest_bytes).hexdigest() == result.manifest_sha256
            for entry in manifest["entries"]:
                content = bundle.read(entry["path"])
                assert len(content) == entry["size"]
                assert sha256(content).hexdigest() == entry["sha256"]
            assert manifest["generated_at"] == account_export.requested_at.isoformat().replace(
                "+00:00", "Z"
            )
        stored = await repository.account_export_for_owner(
            owner_user_id="export-owner",
            export_id=account_export.id,
        )
        assert stored.status == AccountExportStatus.COMPLETED
        assert stored.archive_object_key == export_archive_object_key(
            account_export.account_id,
            account_export.id,
        )

    asyncio.run(scenario())


def test_export_document_strips_operational_secrets_recursively() -> None:
    exported = export_document(
        document_id="capture-1",
        data={
            "account_id": "account-a",
            "object_key": "accounts/account-a/captures/capture-1.jpg",
            "metadata": {
                "token": "must-not-export",
                "safe_note": "steak on Thursday",
            },
            "idempotency_hash": "must-not-export",
        },
    )
    assert exported == {
        "document_id": "capture-1",
        "account_id": "account-a",
        "metadata": {"safe_note": "steak on Thursday"},
    }


def test_export_source_rejects_cross_account_object_keys() -> None:
    with pytest.raises(CrossAccountAccess):
        source_object(
            account_id="account-a",
            collection="captures",
            document_id="capture-1",
            data={
                "account_id": "account-a",
                "object_key": "accounts/account-b/captures/capture-1.jpg",
                "content_type": "image/jpeg",
                "content_sha256": "a" * 64,
            },
        )


def test_terminal_export_failure_is_durable_and_does_not_publish_partial_archive() -> None:
    async def scenario() -> None:
        repository, account_export, stores, snapshot = await prepared_export()
        broken = snapshot.model_copy(
            update={
                "source_objects": (
                    snapshot.source_objects[0].model_copy(
                        update={"content_sha256": "0" * 64}
                    ),
                )
            }
        )
        export_store = InMemoryObjectStore()
        service = AccountExportService(
            repository=repository,
            snapshot_reader=StaticSnapshotReader(broken),
            source_stores=stores,
            export_store=export_store,
        )
        with pytest.raises(ValueError, match="hash mismatch"):
            await service.process(
                account_id=account_export.account_id,
                export_id=account_export.id,
                worker_id="test-worker",
                delivery_attempt=5,
            )
        failed = await repository.account_export_for_owner(
            owner_user_id="export-owner",
            export_id=account_export.id,
        )
        assert failed.status == AccountExportStatus.FAILED
        assert failed.failed_at is not None
        assert failed.archive_object_key is None
        assert export_store._objects == {}

    asyncio.run(scenario())


def test_transient_failure_is_immediately_claimable_on_pubsub_redelivery() -> None:
    async def scenario() -> None:
        repository, account_export, stores, snapshot = await prepared_export()
        reader = FailOnceSnapshotReader(snapshot)
        export_store = InMemoryObjectStore()
        service = AccountExportService(
            repository=repository,
            snapshot_reader=reader,
            source_stores=stores,
            export_store=export_store,
        )
        with pytest.raises(RuntimeError, match="snapshot outage"):
            await service.process(
                account_id=account_export.account_id,
                export_id=account_export.id,
                worker_id="test-worker-1",
                delivery_attempt=1,
            )
        result = await service.process(
            account_id=account_export.account_id,
            export_id=account_export.id,
            worker_id="test-worker-2",
            delivery_attempt=2,
        )
        assert result is not None
        assert reader.calls == 2
        assert len(export_store._objects) == 1

    asyncio.run(scenario())


def test_export_lease_outlives_the_platform_request_envelope() -> None:
    assert EXPORT_LEASE > EXPORT_PLATFORM_REQUEST_ENVELOPE


def test_export_cancellation_aborts_open_streaming_upload() -> None:
    async def scenario() -> None:
        repository, account_export, stores, snapshot = await prepared_export()
        stores["media"] = CancellingSourceStore()
        export_store = TrackingExportStore()
        service = AccountExportService(
            repository=repository,
            snapshot_reader=StaticSnapshotReader(snapshot),
            source_stores=stores,
            export_store=export_store,
        )
        with pytest.raises(asyncio.CancelledError):
            await service.process(
                account_id=account_export.account_id,
                export_id=account_export.id,
                worker_id="cancelled-worker",
                delivery_attempt=1,
            )
        assert export_store.upload_started is True
        assert export_store.abort_called is True
        assert export_store._objects == {}

    asyncio.run(scenario())


def test_export_worker_accepts_pubsub_event_and_deduplicates_redelivery() -> None:
    repository, account_export, stores, snapshot = asyncio.run(prepared_export())
    export_store = InMemoryObjectStore()
    app = create_export_worker_app(
        ExportWorkerSettings(
            environment="test",
            gcp_project_id="test-project",
            media_bucket="test-media",
            raw_mail_bucket="test-mail",
            trace_bucket="test-traces",
            export_bucket="test-exports",
        ),
        repository=repository,
        snapshot_reader=StaticSnapshotReader(snapshot),
        source_stores=stores,
        export_store=export_store,
    )
    payload = {
        "schema_version": 1,
        "kind": "account_export_requested",
        "account_id": account_export.account_id,
        "export_id": account_export.id,
    }
    with TestClient(app) as client:
        assert client.post(
            "/internal/pubsub/account-export-requested",
            json=push_envelope(payload),
        ).status_code == 204
        assert client.post(
            "/internal/pubsub/account-export-requested",
            json=push_envelope(payload),
        ).status_code == 204
    assert len(export_store._objects) == 1


def test_export_worker_retries_active_lease_and_recovers_after_expiry() -> None:
    repository, account_export, stores, snapshot = asyncio.run(prepared_export())
    claimed = asyncio.run(
        repository.claim_account_export(
            account_id=account_export.account_id,
            export_id=account_export.id,
            lease_id="interrupted-worker-lease",
            lease_owner="interrupted-worker",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    assert claimed is not None
    export_store = InMemoryObjectStore()
    app = create_export_worker_app(
        ExportWorkerSettings(
            environment="test",
            gcp_project_id="test-project",
            media_bucket="test-media",
            raw_mail_bucket="test-mail",
            trace_bucket="test-traces",
            export_bucket="test-exports",
        ),
        repository=repository,
        snapshot_reader=StaticSnapshotReader(snapshot),
        source_stores=stores,
        export_store=export_store,
    )
    payload = {
        "schema_version": 1,
        "kind": "account_export_requested",
        "account_id": account_export.account_id,
        "export_id": account_export.id,
    }

    with TestClient(app) as client:
        active_lease = client.post(
            "/internal/pubsub/account-export-requested",
            json=push_envelope(payload, delivery_attempt=2),
        )
        assert active_lease.status_code == 503
        still_building = asyncio.run(
            repository.account_export_for_owner(
                owner_user_id="export-owner",
                export_id=account_export.id,
            )
        )
        assert still_building.status == AccountExportStatus.BUILDING
        job_key = (account_export.account_id, account_export.job_id)
        assert repository._jobs[job_key].status.value == "leased"
        repository._jobs[job_key] = repository._jobs[job_key].model_copy(
            update={"lease_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        recovered = client.post(
            "/internal/pubsub/account-export-requested",
            json=push_envelope(payload, delivery_attempt=3),
        )

    assert recovered.status_code == 204
    stored = asyncio.run(
        repository.account_export_for_owner(
            owner_user_id="export-owner",
            export_id=account_export.id,
        )
    )
    assert stored.status == AccountExportStatus.COMPLETED
    assert len(export_store._objects) == 1


def test_export_worker_rejects_invalid_event_without_retrying() -> None:
    repository, _, stores, snapshot = asyncio.run(prepared_export())
    app = create_export_worker_app(
        ExportWorkerSettings(
            environment="test",
            gcp_project_id="test-project",
            media_bucket="test-media",
            raw_mail_bucket="test-mail",
            trace_bucket="test-traces",
            export_bucket="test-exports",
        ),
        repository=repository,
        snapshot_reader=StaticSnapshotReader(snapshot),
        source_stores=stores,
        export_store=InMemoryObjectStore(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/internal/pubsub/account-export-requested",
            json=push_envelope({"schema_version": 2}),
        )
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_pubsub_event"}
