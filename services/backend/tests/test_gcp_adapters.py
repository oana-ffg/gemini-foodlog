import asyncio

import pytest
from pydantic import ValidationError

from foodlog_backend.app import create_app
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.inference import FixtureInferenceEngine
from foodlog_backend.settings import Settings
from foodlog_backend.storage import GCSObjectStore


class FakeDocument:
    def __init__(self, path: str) -> None:
        self.path = path

    def collection(self, name: str) -> "FakeCollection":
        return FakeCollection(f"{self.path}/{name}")


class FakeCollection:
    def __init__(self, path: str) -> None:
        self.path = path

    def document(self, identifier: str) -> FakeDocument:
        return FakeDocument(f"{self.path}/{identifier}")


class FakeFirestoreClient:
    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(name)


class FakeBlob:
    def __init__(self, key: str, objects: dict[str, tuple[bytes, str]]) -> None:
        self.key = key
        self.objects = objects

    def upload_from_string(
        self,
        content: bytes,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None:
        assert if_generation_match == 0
        if self.key in self.objects:
            raise AssertionError("test adapter forbids overwrite")
        self.objects[self.key] = (content, content_type)

    def download_as_bytes(self) -> bytes:
        return self.objects[self.key][0]

    def delete(self) -> None:
        self.objects.pop(self.key, None)


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def blob(self, key: str) -> FakeBlob:
        return FakeBlob(key, self.objects)


class FakeStorageClient:
    def __init__(self) -> None:
        self.selected_bucket: str | None = None
        self.bucket_instance = FakeBucket()

    def bucket(self, name: str) -> FakeBucket:
        self.selected_bucket = name
        return self.bucket_instance


def test_firestore_paths_are_account_scoped() -> None:
    repository = FirestoreRepository(
        project_id="test-project",
        public_account_limit=25,
        trial_image_limit=200,
        client=FakeFirestoreClient(),  # type: ignore[arg-type]
    )

    assert repository._collection("account-a", "captures").path == ("accounts/account-a/captures")
    assert repository._entitlement("account-a").path == ("accounts/account-a/entitlements/current")


def test_gcs_adapter_writes_once_and_round_trips_private_bytes() -> None:
    client = FakeStorageClient()
    store = GCSObjectStore(
        project_id="test-project",
        bucket_name="private-media",
        client=client,  # type: ignore[arg-type]
    )
    key = "accounts/account-a/captures/capture-a.jpg"

    asyncio.run(store.put(key, b"image-bytes", "image/jpeg"))

    assert client.selected_bucket == "private-media"
    assert asyncio.run(store.get(key)) == b"image-bytes"
    assert client.bucket_instance.objects[key][1] == "image/jpeg"
    asyncio.run(store.delete(key))
    assert key not in client.bucket_instance.objects


def test_production_cannot_select_partial_or_volatile_storage() -> None:
    with pytest.raises(ValidationError, match="in-memory storage"):
        Settings(environment="production", storage_backend="memory")
    with pytest.raises(ValidationError, match="gcp_project_id and media_bucket"):
        Settings(environment="production", storage_backend="gcp")

    settings = Settings(
        environment="production",
        auth_backend="firebase",
        storage_backend="gcp",
        gcp_project_id="gemini-foodlog-2026",
        firebase_project_id="gemini-foodlog-2026",
        media_bucket="gemini-foodlog-2026-media-163029863855",
    )
    assert settings.storage_backend == "gcp"


def test_production_requires_explicit_non_fixture_inference() -> None:
    settings = Settings(
        environment="production",
        auth_backend="firebase",
        storage_backend="gcp",
        gcp_project_id="gemini-foodlog-2026",
        firebase_project_id="gemini-foodlog-2026",
        media_bucket="gemini-foodlog-2026-media-163029863855",
    )
    with pytest.raises(ValueError, match="Production requires"):
        create_app(settings)
    with pytest.raises(ValueError, match="Production requires"):
        create_app(settings, inference_engine=FixtureInferenceEngine())
