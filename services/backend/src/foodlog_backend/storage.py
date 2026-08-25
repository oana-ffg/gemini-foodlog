import asyncio
from typing import Protocol

from google.api_core.exceptions import NotFound
from google.cloud import storage


class ObjectStore(Protocol):
    async def put(self, key: str, content: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class InMemoryObjectStore:
    """Local-only object storage adapter; production must use private GCS."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        async with self._lock:
            self._objects[key] = content

    async def get(self, key: str) -> bytes:
        async with self._lock:
            return self._objects[key]

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._objects.pop(key, None)


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

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        blob = self._bucket.blob(key)
        await asyncio.to_thread(
            blob.upload_from_string,
            content,
            content_type=content_type,
            if_generation_match=0,
        )

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._bucket.blob(key).download_as_bytes)

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._bucket.blob(key).delete)
        except NotFound:
            return
