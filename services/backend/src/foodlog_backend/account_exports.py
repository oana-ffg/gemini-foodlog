import json
import shutil
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from tempfile import TemporaryFile
from typing import Literal, Protocol
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import AccountExport
from .repository import Repository
from .storage import ObjectStore, file_sha256

EXPORT_FORMAT_VERSION = "foodlog-account-export-v1"
EXPORT_CONTENT_TYPE = "application/zip"
EXPORT_EXPIRY = timedelta(hours=24)
# Cloud Run and Pub/Sub both allow the request 600 seconds. Keep the durable lease
# beyond that hard envelope: synchronous ZIP compression and resumable-upload writes
# cannot be cooperatively cancelled, so an early application timeout would permit an
# overlapping retry while the original writer was still alive.
EXPORT_PLATFORM_REQUEST_ENVELOPE = timedelta(minutes=10)
EXPORT_LEASE = timedelta(minutes=12)
EXPORT_MAX_DELIVERY_ATTEMPTS = 5


def validate_archive_path(path: str) -> str:
    segments = path.split("/")
    if (
        not path
        or path.startswith("/")
        or path.endswith("/")
        or any(segment in {"", ".", ".."} or "\\" in segment for segment in segments)
    ):
        raise ValueError("account export archive path is unsafe")
    return path


class ExportJsonFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    content: bytes = Field(max_length=10_000_000)

    @model_validator(mode="after")
    def path_is_safe_json(self) -> "ExportJsonFile":
        validate_archive_path(self.path)
        if not self.path.endswith(".json"):
            raise ValueError("account export JSON entries require a .json path")
        return self


class ExportSourceObject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_path: str = Field(min_length=1, max_length=512)
    source: Literal["media", "raw_mail", "traces"]
    object_key: str = Field(min_length=1, max_length=512)
    content_type: str = Field(min_length=1, max_length=200)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int | None = Field(default=None, ge=0)
    generation: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def archive_path_is_safe(self) -> "ExportSourceObject":
        validate_archive_path(self.archive_path)
        return self


class AccountExportSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str = Field(min_length=1, max_length=128)
    export_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_at: datetime
    json_files: tuple[ExportJsonFile, ...]
    source_objects: tuple[ExportSourceObject, ...]

    @model_validator(mode="after")
    def paths_are_unique(self) -> "AccountExportSnapshot":
        paths = [item.path for item in self.json_files] + [
            item.archive_path for item in self.source_objects
        ]
        if len(paths) != len(set(paths)) or "manifest.json" in paths:
            raise ValueError("account export snapshot paths must be unique")
        if self.snapshot_at.tzinfo is None or self.snapshot_at.utcoffset() is None:
            raise ValueError("account export snapshot time must include a UTC offset")
        return self


class ExportManifestEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: Literal["json", "media", "raw_mail", "trace"]
    content_type: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_generation: int | None = Field(default=None, ge=1)


class ExportManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: Literal["foodlog-account-export-v1"] = EXPORT_FORMAT_VERSION
    export_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_at: datetime
    generated_at: datetime
    entries: tuple[ExportManifestEntry, ...]


class ExportArchiveResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    object_key: str
    archive_size: int = Field(ge=1)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    expires_at: datetime


class AccountExportSnapshotReader(Protocol):
    async def read(self, account_export: AccountExport) -> AccountExportSnapshot: ...


def canonical_json(value: BaseModel | dict[str, object] | list[object]) -> bytes:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", exclude_none=True)
    else:
        payload = value
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def export_archive_object_key(account_id: str, export_id: str) -> str:
    return f"accounts/{account_id}/exports/{export_id}.zip"


def _zip_info(path: str, snapshot_at: datetime) -> ZipInfo:
    normalized = snapshot_at.astimezone(UTC)
    safe_year = max(1980, normalized.year)
    info = ZipInfo(
        filename=path,
        date_time=(
            safe_year,
            normalized.month,
            normalized.day,
            normalized.hour,
            normalized.minute,
            normalized.second - normalized.second % 2,
        ),
    )
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    info.create_system = 3
    return info


class AccountExportService:
    def __init__(
        self,
        *,
        repository: Repository,
        snapshot_reader: AccountExportSnapshotReader,
        source_stores: dict[str, ObjectStore],
        export_store: ObjectStore,
    ) -> None:
        if set(source_stores) != {"media", "raw_mail", "traces"}:
            raise ValueError("account export requires exactly the three retained source stores")
        self._repository = repository
        self._snapshot_reader = snapshot_reader
        self._source_stores = source_stores
        self._export_store = export_store

    async def process(
        self,
        *,
        account_id: str,
        export_id: str,
        worker_id: str,
        delivery_attempt: int,
    ) -> ExportArchiveResult | None:
        lease_id = str(uuid4())
        claimed = await self._repository.claim_account_export(
            account_id=account_id,
            export_id=export_id,
            lease_id=lease_id,
            lease_owner=worker_id,
            lease_expires_at=datetime.now(UTC) + EXPORT_LEASE,
        )
        if claimed is None:
            return None
        try:
            snapshot = await self._snapshot_reader.read(claimed)
            if (
                snapshot.account_id != account_id
                or snapshot.export_id != export_id
                or snapshot.snapshot_at != claimed.snapshot_at
            ):
                raise ValueError("account export snapshot identity mismatch")
            result = await self._build_and_store(claimed, snapshot)
            completed = await self._repository.complete_account_export(
                account_id=account_id,
                export_id=export_id,
                lease_id=lease_id,
                archive_object_key=result.object_key,
                archive_size=result.archive_size,
                archive_sha256=result.archive_sha256,
                manifest_sha256=result.manifest_sha256,
                completed_at=result.completed_at,
                expires_at=result.expires_at,
            )
            if completed is None:
                raise RuntimeError("account export lease changed before completion")
            return result
        except Exception as error:
            error_code = type(error).__name__[:120]
            now = datetime.now(UTC)
            if delivery_attempt >= EXPORT_MAX_DELIVERY_ATTEMPTS:
                await self._repository.fail_account_export(
                    account_id=account_id,
                    export_id=export_id,
                    lease_id=lease_id,
                    error_code=error_code,
                    failed_at=now,
                )
            else:
                await self._repository.release_account_export(
                    account_id=account_id,
                    export_id=export_id,
                    lease_id=lease_id,
                    # Pub/Sub owns retry timing. Keeping a second application
                    # delay can turn an early redelivery into a premature ACK.
                    available_at=now,
                    error_code=error_code,
                )
            raise

    async def _build_and_store(
        self,
        account_export: AccountExport,
        snapshot: AccountExportSnapshot,
    ) -> ExportArchiveResult:
        completed_at = datetime.now(UTC)
        entries: list[ExportManifestEntry] = []
        object_key = export_archive_object_key(account_export.account_id, account_export.id)
        upload = await self._export_store.start_streaming_put(
            account_export.account_id,
            object_key,
            EXPORT_CONTENT_TYPE,
        )
        try:
            with ZipFile(
                upload.writer,  # type: ignore[arg-type]
                mode="w",
                compression=ZIP_DEFLATED,
                compresslevel=6,
                strict_timestamps=False,
            ) as bundle:
                for item in sorted(snapshot.json_files, key=lambda candidate: candidate.path):
                    content_hash = sha256(item.content).hexdigest()
                    bundle.writestr(_zip_info(item.path, snapshot.snapshot_at), item.content)
                    entries.append(
                        ExportManifestEntry(
                            path=item.path,
                            kind="json",
                            content_type="application/json",
                            size=len(item.content),
                            sha256=content_hash,
                        )
                    )

                for item in sorted(
                    snapshot.source_objects,
                    key=lambda candidate: candidate.archive_path,
                ):
                    source_store = self._source_stores[item.source]
                    with TemporaryFile("w+b") as source:
                        await source_store.download_to_file(
                            account_export.account_id,
                            item.object_key,
                            source,
                        )
                        source.seek(0, 2)
                        size = source.tell()
                        source.seek(0)
                        content_hash = file_sha256(source)
                        if content_hash != item.content_sha256:
                            raise ValueError("account export source object hash mismatch")
                        if item.size is not None and size != item.size:
                            raise ValueError("account export source object size mismatch")
                        with bundle.open(
                            _zip_info(item.archive_path, snapshot.snapshot_at),
                            mode="w",
                        ) as destination:
                            shutil.copyfileobj(source, destination, length=1024 * 1024)
                    kind = "trace" if item.source == "traces" else item.source
                    entries.append(
                        ExportManifestEntry(
                            path=item.archive_path,
                            kind=kind,
                            content_type=item.content_type,
                            size=size,
                            sha256=content_hash,
                            source_generation=item.generation,
                        )
                    )

                manifest = ExportManifest(
                    export_id=account_export.id,
                    snapshot_at=snapshot.snapshot_at,
                    # Keep retries byte-for-byte deterministic. The completion time lives
                    # on the export record, while the archive describes its fixed snapshot.
                    generated_at=account_export.requested_at,
                    entries=tuple(sorted(entries, key=lambda entry: entry.path)),
                )
                manifest_content = canonical_json(manifest)
                manifest_sha256 = sha256(manifest_content).hexdigest()
                bundle.writestr(
                    _zip_info("manifest.json", snapshot.snapshot_at),
                    manifest_content,
                )
            archive_size = upload.size
            archive_sha256 = upload.content_sha256
            await self._export_store.finish_streaming_put(upload)
        except BaseException:
            await self._export_store.abort_streaming_put(upload)
            raise
        return ExportArchiveResult(
            object_key=object_key,
            archive_size=archive_size,
            archive_sha256=archive_sha256,
            manifest_sha256=manifest_sha256,
            completed_at=completed_at,
            expires_at=completed_at + EXPORT_EXPIRY,
        )
