import asyncio
from datetime import timedelta

import pytest
from google.api_core.exceptions import PreconditionFailed
from pydantic import ValidationError

from foodlog_backend.firestore_repository import FirestoreRepository, _model
from foodlog_backend.models import ActivityEvent, EntitlementMode, utc_now
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
            raise PreconditionFailed("object already exists")
        self.objects[self.key] = (content, content_type)

    @property
    def content_type(self) -> str:
        return self.objects[self.key][1]

    def reload(self) -> None:
        return None

    def download_as_bytes(self) -> bytes:
        return self.objects[self.key][0]


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


class FakeSnapshot:
    def __init__(self, identifier: str, data: dict) -> None:
        self.id = identifier
        self._data = data

    def get(self, field: str):
        if field not in self._data:
            raise KeyError(field)
        return self._data[field]

    def to_dict(self) -> dict:
        return dict(self._data)


def test_firestore_paths_are_account_scoped() -> None:
    repository = FirestoreRepository(
        project_id="test-project",
        public_account_limit=25,
        trial_image_limit=200,
        client=FakeFirestoreClient(),  # type: ignore[arg-type]
    )

    assert repository._collection("account-a", "captures").path == ("accounts/account-a/captures")
    assert repository._entitlement("account-a").path == ("accounts/account-a/entitlements/current")


def test_firestore_activity_event_preserves_its_materialized_update_time() -> None:
    created_at = utc_now()
    updated_at = created_at + timedelta(minutes=1)
    event = _model(
        FakeSnapshot(
            "event-a",
            {
                "schema_version": 1,
                "id": "event-a",
                "account_id": "account-a",
                "status": "open",
                "current_revision": 1,
                "camera_ids": ["camera-a"],
                "first_capture_at": created_at,
                "last_capture_at": updated_at,
                "capture_count": 2,
                "grouping_policy_version": "temporal-v1",
                "meal_id": None,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        ),
        ActivityEvent,
    )

    assert event.updated_at == updated_at


def test_legacy_trial_entitlement_without_mode_remains_readable() -> None:
    created_at = utc_now()
    account = FirestoreRepository._account_from_snapshots(
        FakeSnapshot(
            "legacy-account",
            {
                "owner_user_id": "legacy-owner",
                "status": "active",
                "created_at": created_at,
            },
        ),
        FakeSnapshot(
            "current",
            {
                "accepted_image_count": 1,
                "trial_image_limit": 200,
            },
        ),
    )

    assert account.entitlement_mode == EntitlementMode.TRIAL
    assert account.trial_image_limit == 200
    assert account.accepted_image_count == 1


def test_gcs_adapter_writes_once_and_round_trips_private_bytes() -> None:
    client = FakeStorageClient()
    store = GCSObjectStore(
        project_id="test-project",
        bucket_name="private-media",
        client=client,  # type: ignore[arg-type]
    )
    key = "accounts/account-a/captures/capture-a.jpg"

    created = asyncio.run(store.put(key, b"image-bytes", "image/jpeg"))
    duplicate = asyncio.run(store.put(key, b"image-bytes", "image/jpeg"))

    assert client.selected_bucket == "private-media"
    assert created is True
    assert duplicate is False
    assert asyncio.run(store.get(key)) == b"image-bytes"
    assert client.bucket_instance.objects[key][1] == "image/jpeg"


def test_gcs_adapter_never_accepts_different_bytes_for_an_existing_key() -> None:
    client = FakeStorageClient()
    store = GCSObjectStore(
        project_id="test-project",
        bucket_name="private-media",
        client=client,  # type: ignore[arg-type]
    )
    key = "accounts/account-a/captures/capture-a.jpg"
    asyncio.run(store.put(key, b"original", "image/jpeg"))

    with pytest.raises(ValueError, match="different content"):
        asyncio.run(store.put(key, b"replacement", "image/jpeg"))

    assert asyncio.run(store.get(key)) == b"original"


def test_production_cannot_select_partial_or_volatile_storage() -> None:
    with pytest.raises(ValidationError, match="in-memory storage"):
        Settings(environment="production", storage_backend="memory")
    with pytest.raises(ValidationError, match="gcp_project_id and media_bucket"):
        Settings(environment="production", storage_backend="gcp")
    with pytest.raises(ValidationError, match="account notification topic"):
        Settings(
            environment="production",
            auth_backend="firebase",
            storage_backend="gcp",
            gcp_project_id="gemini-foodlog-2026",
            firebase_project_id="gemini-foodlog-2026",
            media_bucket="gemini-foodlog-2026-media-163029863855",
        )

    settings = Settings(
        environment="production",
        auth_backend="firebase",
        storage_backend="gcp",
        gcp_project_id="gemini-foodlog-2026",
        firebase_project_id="gemini-foodlog-2026",
        media_bucket="gemini-foodlog-2026-media-163029863855",
        notification_topic=("projects/gemini-foodlog-2026/topics/foodlog-notification-events"),
    )
    assert settings.storage_backend == "gcp"


def test_grouping_policy_settings_are_typed_and_deployment_configurable() -> None:
    settings = Settings(
        environment="test",
        grouping_policy_version="temporal-v2",
        grouping_quiet_seconds=45,
        grouping_reopen_seconds=5_400,
    )

    assert settings.grouping_policy_version == "temporal-v2"
    assert settings.grouping_quiet_seconds == 45
    assert settings.grouping_reopen_seconds == 5_400

    with pytest.raises(ValidationError, match="must not be shorter"):
        Settings(
            environment="test",
            grouping_quiet_seconds=60,
            grouping_reopen_seconds=30,
        )
