import asyncio
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from tempfile import TemporaryFile
from typing import BinaryIO, Protocol

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

from .errors import CrossAccountAccess


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    size: int
    content_type: str | None
    generation: int | None
    crc32c: str | None
    updated_at: datetime | None


def validate_account_object_key(account_id: str, key: str) -> None:
    """Reject object paths that do not belong exactly to the active account."""
    if not account_id or "/" in account_id or "\\" in account_id:
        raise CrossAccountAccess
    segments = key.split("/")
    if (
        len(segments) < 4
        or segments[0] != "accounts"
        or segments[1] != account_id
        or any(segment in {"", ".", ".."} or "\\" in segment for segment in segments)
    ):
        raise CrossAccountAccess


class ObjectStore(Protocol):
    async def put(
        self,
        account_id: str,
        key: str,
        content: bytes,
        content_type: str,
    ) -> bool: ...

    async def get(self, account_id: str, key: str) -> bytes: ...

    async def metadata(self, account_id: str, key: str) -> ObjectMetadata: ...

    async def put_file(
        self,
        account_id: str,
        key: str,
        source: BinaryIO,
        content_type: str,
        content_sha256: str,
    ) -> bool: ...

    async def download_to_file(
        self,
        account_id: str,
        key: str,
        destination: BinaryIO,
    ) -> None: ...


def file_sha256(source: BinaryIO) -> str:
    position = source.tell()
    source.seek(0)
    digest = sha256()
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
    source.seek(position)
    return digest.hexdigest()


class InMemoryObjectStore:
    """Local-only object storage adapter; production must use private GCS."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        account_id: str,
        key: str,
        content: bytes,
        content_type: str,
    ) -> bool:
        validate_account_object_key(account_id, key)
        async with self._lock:
            existing = self._objects.get(key)
            if existing is not None:
                if existing != (content, content_type):
                    raise ValueError("Object key already contains different content")
                return False
            self._objects[key] = (content, content_type)
            return True

    async def get(self, account_id: str, key: str) -> bytes:
        validate_account_object_key(account_id, key)
        async with self._lock:
            return self._objects[key][0]

    async def metadata(self, account_id: str, key: str) -> ObjectMetadata:
        validate_account_object_key(account_id, key)
        async with self._lock:
            content, content_type = self._objects[key]
            return ObjectMetadata(
                key=key,
                size=len(content),
                content_type=content_type,
                generation=None,
                crc32c=None,
                updated_at=None,
            )

    async def put_file(
        self,
        account_id: str,
        key: str,
        source: BinaryIO,
        content_type: str,
        content_sha256: str,
    ) -> bool:
        if file_sha256(source) != content_sha256:
            raise ValueError("File hash does not match the declared content SHA-256")
        position = source.tell()
        source.seek(0)
        content = source.read()
        source.seek(position)
        return await self.put(account_id, key, content, content_type)

    async def download_to_file(
        self,
        account_id: str,
        key: str,
        destination: BinaryIO,
    ) -> None:
        content = await self.get(account_id, key)
        destination.write(content)
        destination.seek(0)


class GCSObjectStore:
    """Private GCS adapter; callers supply only server-derived object keys."""

    def __init__(
        self,
        *,
        project_id: str,
        bucket_name: str,
        client: storage.Client | None = None,
    ) -> None:
        self._client = client or storage.Client(project=project_id)
        self._bucket = self._client.bucket(bucket_name)

    async def put(
        self,
        account_id: str,
        key: str,
        content: bytes,
        content_type: str,
    ) -> bool:
        validate_account_object_key(account_id, key)
        blob = self._bucket.blob(key)
        try:
            await asyncio.to_thread(
                blob.upload_from_string,
                content,
                content_type=content_type,
                if_generation_match=0,
            )
            return True
        except PreconditionFailed:
            existing = await asyncio.to_thread(blob.download_as_bytes)
            await asyncio.to_thread(blob.reload)
            if existing != content or blob.content_type != content_type:
                raise ValueError("Object key already contains different content") from None
            return False

    async def get(self, account_id: str, key: str) -> bytes:
        validate_account_object_key(account_id, key)
        return await asyncio.to_thread(self._bucket.blob(key).download_as_bytes)

    async def metadata(self, account_id: str, key: str) -> ObjectMetadata:
        validate_account_object_key(account_id, key)
        blob = self._bucket.blob(key)
        await asyncio.to_thread(blob.reload)
        return ObjectMetadata(
            key=key,
            size=blob.size,
            content_type=blob.content_type,
            generation=int(blob.generation) if blob.generation is not None else None,
            crc32c=blob.crc32c,
            updated_at=blob.updated,
        )

    async def put_file(
        self,
        account_id: str,
        key: str,
        source: BinaryIO,
        content_type: str,
        content_sha256: str,
    ) -> bool:
        validate_account_object_key(account_id, key)
        if file_sha256(source) != content_sha256:
            raise ValueError("File hash does not match the declared content SHA-256")
        blob = self._bucket.blob(key)
        source.seek(0)
        try:
            await asyncio.to_thread(
                blob.upload_from_file,
                source,
                content_type=content_type,
                if_generation_match=0,
                rewind=True,
            )
            return True
        except PreconditionFailed:
            await asyncio.to_thread(blob.reload)
            if blob.content_type != content_type:
                raise ValueError("Object key already contains different content") from None
            with TemporaryFile("w+b") as existing:
                await asyncio.to_thread(blob.download_to_file, existing)
                if file_sha256(existing) != content_sha256:
                    raise ValueError("Object key already contains different content") from None
            return False

    async def download_to_file(
        self,
        account_id: str,
        key: str,
        destination: BinaryIO,
    ) -> None:
        validate_account_object_key(account_id, key)
        await asyncio.to_thread(self._bucket.blob(key).download_to_file, destination)
        destination.seek(0)
