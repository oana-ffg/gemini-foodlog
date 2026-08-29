import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from typing import BinaryIO, Protocol

from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

from .errors import CrossAccountAccess


async def _finish_thread_call[ResultT](
    function: Callable[..., ResultT], /, *args: object
) -> ResultT:
    """Let an irreversible blocking finalizer settle before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    size: int
    content_type: str | None
    generation: int | None
    crc32c: str | None
    updated_at: datetime | None


class _StreamingDestination(Protocol):
    def write(self, content: bytes) -> int | None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class _StreamingUploadWriter:
    """Hash and count a non-seekable object upload without retaining prior bytes."""

    def __init__(self, destination: _StreamingDestination) -> None:
        self._destination = destination
        self._digest = sha256()
        self._size = 0

    @property
    def content_sha256(self) -> str:
        return self._digest.hexdigest()

    @property
    def size(self) -> int:
        return self._size

    def write(self, content: bytes) -> int:
        written = self._destination.write(content)
        if written is None:
            written = len(content)
        if written != len(content):
            raise OSError("object upload accepted only part of a write")
        self._digest.update(content)
        self._size += written
        return written

    def tell(self) -> int:
        return self._size

    def flush(self) -> None:
        self._destination.flush()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


class _ExistingObjectWriter:
    """Verify a deterministic retry against an existing object as bytes are rebuilt."""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source

    def write(self, content: bytes) -> int:
        existing = self._source.read(len(content))
        if existing != content:
            raise ValueError("Object key already contains different content")
        return len(content)

    def flush(self) -> None:
        return None

    def finish(self) -> None:
        if self._source.read(1):
            raise ValueError("Object key already contains different content")

    def close(self) -> None:
        self._source.close()


@dataclass
class StreamingObjectUpload:
    account_id: str
    key: str
    content_type: str
    writer: _StreamingUploadWriter
    _destination: _StreamingDestination
    _existing: _ExistingObjectWriter | None = None
    _finalized: bool = False

    @property
    def content_sha256(self) -> str:
        return self.writer.content_sha256

    @property
    def size(self) -> int:
        return self.writer.size


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

    async def start_streaming_put(
        self,
        account_id: str,
        key: str,
        content_type: str,
    ) -> StreamingObjectUpload: ...

    async def finish_streaming_put(self, upload: StreamingObjectUpload) -> bool: ...

    async def abort_streaming_put(self, upload: StreamingObjectUpload) -> None: ...

    async def download_to_file(
        self,
        account_id: str,
        key: str,
        destination: BinaryIO,
    ) -> None: ...

    def iter_range(
        self,
        account_id: str,
        key: str,
        *,
        start: int,
        end: int,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes]: ...


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

    async def start_streaming_put(
        self,
        account_id: str,
        key: str,
        content_type: str,
    ) -> StreamingObjectUpload:
        validate_account_object_key(account_id, key)
        async with self._lock:
            existing = self._objects.get(key)
        if existing is None:
            destination = BytesIO()
            return StreamingObjectUpload(
                account_id=account_id,
                key=key,
                content_type=content_type,
                writer=_StreamingUploadWriter(destination),
                _destination=destination,
            )
        content, existing_content_type = existing
        if existing_content_type != content_type:
            raise ValueError("Object key already contains different content")
        verifier = _ExistingObjectWriter(BytesIO(content))
        return StreamingObjectUpload(
            account_id=account_id,
            key=key,
            content_type=content_type,
            writer=_StreamingUploadWriter(verifier),
            _destination=verifier,
            _existing=verifier,
        )

    async def finish_streaming_put(self, upload: StreamingObjectUpload) -> bool:
        if upload._existing is not None:
            try:
                upload._existing.finish()
            finally:
                upload._existing.close()
            upload._finalized = True
            return False
        destination = upload._destination
        if not isinstance(destination, BytesIO):
            raise TypeError("in-memory upload has an invalid destination")
        content = destination.getvalue()
        destination.close()
        if sha256(content).hexdigest() != upload.content_sha256:
            raise ValueError("streaming upload hash mismatch")
        created = await self.put(
            upload.account_id,
            upload.key,
            content,
            upload.content_type,
        )
        upload._finalized = True
        return created

    async def abort_streaming_put(self, upload: StreamingObjectUpload) -> None:
        if upload._finalized:
            return
        upload._destination.close()

    async def download_to_file(
        self,
        account_id: str,
        key: str,
        destination: BinaryIO,
    ) -> None:
        content = await self.get(account_id, key)
        destination.write(content)
        destination.seek(0)

    async def iter_range(
        self,
        account_id: str,
        key: str,
        *,
        start: int,
        end: int,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes]:
        if chunk_size < 1 or start < 0 or end < start:
            raise ValueError("invalid object byte range")
        content = await self.get(account_id, key)
        if end >= len(content):
            raise ValueError("object byte range exceeds content")
        for offset in range(start, end + 1, chunk_size):
            yield content[offset : min(offset + chunk_size, end + 1)]


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

    async def start_streaming_put(
        self,
        account_id: str,
        key: str,
        content_type: str,
    ) -> StreamingObjectUpload:
        validate_account_object_key(account_id, key)
        blob = self._bucket.blob(key)
        if await asyncio.to_thread(blob.exists):
            await asyncio.to_thread(blob.reload)
            if blob.content_type != content_type:
                raise ValueError("Object key already contains different content")
            source = await asyncio.to_thread(blob.open, "rb")
            verifier = _ExistingObjectWriter(source)
            return StreamingObjectUpload(
                account_id=account_id,
                key=key,
                content_type=content_type,
                writer=_StreamingUploadWriter(verifier),
                _destination=verifier,
                _existing=verifier,
            )
        destination = await asyncio.to_thread(
            blob.open,
            "wb",
            chunk_size=8 * 1024 * 1024,
            ignore_flush=True,
            content_type=content_type,
            if_generation_match=0,
        )
        return StreamingObjectUpload(
            account_id=account_id,
            key=key,
            content_type=content_type,
            writer=_StreamingUploadWriter(destination),
            _destination=destination,
        )

    async def finish_streaming_put(self, upload: StreamingObjectUpload) -> bool:
        if upload._existing is not None:
            try:
                await _finish_thread_call(upload._existing.finish)
            finally:
                await _finish_thread_call(upload._existing.close)
            upload._finalized = True
            return False
        # Closing commits the resumable upload. If the request is cancelled,
        # wait for that already-started close to settle before cleanup decides
        # whether a deterministic retry must verify an existing object.
        try:
            await _finish_thread_call(upload._destination.close)
        except asyncio.CancelledError:
            upload._finalized = True
            raise
        upload._finalized = True
        return True

    async def abort_streaming_put(self, upload: StreamingObjectUpload) -> None:
        if upload._finalized:
            return
        if upload._existing is not None:
            await asyncio.to_thread(upload._existing.close)
            return
        terminate = getattr(upload._destination, "terminate", None)
        if terminate is not None:
            await asyncio.to_thread(terminate)
        else:
            await asyncio.to_thread(upload._destination.close)

    async def download_to_file(
        self,
        account_id: str,
        key: str,
        destination: BinaryIO,
    ) -> None:
        validate_account_object_key(account_id, key)
        await asyncio.to_thread(self._bucket.blob(key).download_to_file, destination)
        destination.seek(0)

    async def iter_range(
        self,
        account_id: str,
        key: str,
        *,
        start: int,
        end: int,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes]:
        validate_account_object_key(account_id, key)
        if chunk_size < 1 or start < 0 or end < start:
            raise ValueError("invalid object byte range")
        reader = await asyncio.to_thread(self._bucket.blob(key).open, "rb")
        try:
            await asyncio.to_thread(reader.seek, start)
            remaining = end - start + 1
            while remaining:
                content = await asyncio.to_thread(reader.read, min(chunk_size, remaining))
                if not content:
                    raise ValueError("object ended before the requested byte range")
                remaining -= len(content)
                yield content
        finally:
            await asyncio.to_thread(reader.close)
