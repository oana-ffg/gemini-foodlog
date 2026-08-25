import asyncio


class InMemoryObjectStore:
    """Local-only object storage adapter; production must use private GCS."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def put(self, key: str, content: bytes) -> None:
        async with self._lock:
            self._objects[key] = content

    async def get(self, key: str) -> bytes:
        async with self._lock:
            return self._objects[key]

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._objects.pop(key, None)
