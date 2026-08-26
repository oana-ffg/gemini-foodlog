import asyncio
import json
from base64 import b64decode
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from foodlog_backend.app import create_app, image_dimensions
from foodlog_backend.errors import AccountCapacityReached
from foodlog_backend.inference import FixtureInferenceEngine, verify_fixture_files
from foodlog_backend.models import CaptureEnvelopeV1, MealEntry, utc_now
from foodlog_backend.settings import Settings

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "images"
ADVERSARIAL_FIXTURES = FIXTURES / "adversarial"
USER_HEADER = {"X-FoodLog-Local-User": "owner-a"}


def build_client() -> TestClient:
    return TestClient(create_app(Settings(environment="test")))


def test_concurrent_account_provisioning_is_idempotent() -> None:
    app = create_app(Settings(environment="test"))
    repository = app.state.container.repository

    async def provision_concurrently() -> list:
        return await asyncio.gather(
            *(repository.provision_account("same-owner") for _ in range(50))
        )

    accounts = asyncio.run(provision_concurrently())

    assert len({account.id for account in accounts}) == 1
    assert all(account.owner_user_id == "same-owner" for account in accounts)


def test_concurrent_public_admission_never_exceeds_the_configured_limit() -> None:
    app = create_app(Settings(environment="test", public_account_limit=25))
    repository = app.state.container.repository

    async def provision_competing_users() -> list:
        return await asyncio.gather(
            *(repository.provision_account(f"competing-owner-{index}") for index in range(50)),
            return_exceptions=True,
        )

    results = asyncio.run(provision_competing_users())
    admitted = [result for result in results if not isinstance(result, Exception)]
    rejected = [result for result in results if isinstance(result, Exception)]

    assert len(admitted) == 25
    assert len({account.id for account in admitted}) == 25
    assert len(rejected) == 25
    assert all(isinstance(error, AccountCapacityReached) for error in rejected)


def test_capacity_response_is_stable_and_admitted_user_remains_idempotent() -> None:
    app = create_app(Settings(environment="test", public_account_limit=1))
    with TestClient(app) as client:
        admitted = client.post(
            "/v1/accounts",
            headers={"X-FoodLog-Local-User": "admitted-owner"},
        )
        overflow = client.post(
            "/v1/accounts",
            headers={"X-FoodLog-Local-User": "waitlisted-owner"},
        )
        retry = client.post(
            "/v1/accounts",
            headers={"X-FoodLog-Local-User": "admitted-owner"},
        )

    assert admitted.status_code == retry.status_code == 200
    assert admitted.json()["id"] == retry.json()["id"]
    assert overflow.status_code == 409
    assert overflow.json() == {"detail": "signup_capacity_exhausted"}


def test_public_accounts_receive_the_configured_trial_and_exhaust_it() -> None:
    app = create_app(Settings(environment="test", trial_image_limit=1))
    with TestClient(app) as client:
        account, camera = provision(client, "public-trial-owner")
        image = (FIXTURES / "synthetic-leftover-pasta.png").read_bytes()
        first = post_shared_browser_capture(
            client,
            camera=camera,
            image=image,
            idempotency_key="public-trial-capture-0001",
            user="public-trial-owner",
        )
        exhausted = post_shared_browser_capture(
            client,
            camera=camera,
            image=image,
            idempotency_key="public-trial-capture-0002",
            user="public-trial-owner",
        )

    assert account["entitlement_mode"] == "trial"
    assert account["trial_image_limit"] == 1
    assert first.status_code == 202
    assert first.json()["entitlement_mode"] == "trial"
    assert first.json()["trial_image_limit"] == 1
    assert exhausted.status_code == 429
    assert exhausted.json() == {"detail": "trial_image_quota_exhausted"}


def test_new_public_account_defaults_to_200_images() -> None:
    with build_client() as client:
        account = client.post("/v1/accounts", headers=USER_HEADER)

    assert account.status_code == 200
    assert account.json()["entitlement_mode"] == "trial"
    assert account.json()["trial_image_limit"] == 200


def test_explicit_unlimited_account_has_no_fake_limit_or_public_slot() -> None:
    settings = Settings(
        environment="test",
        public_account_limit=1,
        trial_image_limit=1,
        unlimited_owner_user_ids={"internal-owner"},
    )
    app = create_app(settings)
    with TestClient(app) as client:
        public_account = client.post(
            "/v1/accounts",
            headers={"X-FoodLog-Local-User": "public-owner"},
        )
        internal_account, camera = provision(client, "internal-owner")
        overflow = client.post(
            "/v1/accounts",
            headers={"X-FoodLog-Local-User": "overflow-owner"},
        )
        image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
        captures = [
            post_shared_browser_capture(
                client,
                camera=camera,
                image=image,
                idempotency_key=f"unlimited-capture-{index:04d}",
                user="internal-owner",
            )
            for index in range(2)
        ]

    assert public_account.status_code == 200
    assert internal_account["entitlement_mode"] == "unlimited"
    assert internal_account["trial_image_limit"] is None
    assert overflow.status_code == 409
    assert [capture.status_code for capture in captures] == [202, 202]
    assert captures[-1].json()["accepted_image_count"] == 2
    assert captures[-1].json()["entitlement_mode"] == "unlimited"
    assert captures[-1].json()["trial_image_limit"] is None


def test_preview_requires_both_iam_defense_and_shared_secret() -> None:
    settings = Settings(
        environment="preview",
        preview_shared_secret="preview-secret-that-is-at-least-32-characters",
    )
    with TestClient(create_app(settings)) as client:
        missing_secret = client.post("/v1/accounts", headers=USER_HEADER)
        wrong_secret = client.post(
            "/v1/accounts",
            headers={**USER_HEADER, "X-FoodLog-Preview-Secret": "x" * 32},
        )
        accepted = client.post(
            "/v1/accounts",
            headers={
                **USER_HEADER,
                "X-FoodLog-Preview-Secret": settings.preview_shared_secret,
            },
        )

        assert missing_secret.status_code == 401
        assert wrong_secret.status_code == 401
        assert accepted.status_code == 200


def provision(client: TestClient, user: str = "owner-a") -> tuple[dict, dict]:
    headers = {"X-FoodLog-Local-User": user}
    account = client.post("/v1/accounts", headers=headers)
    assert account.status_code == 200
    camera = client.post(
        "/v1/browser-cameras",
        headers=headers,
        json={
            "name": "Test browser camera",
            "client_instance_id": f"test-browser-{user}-0001",
        },
    )
    assert camera.status_code == 200
    return account.json(), camera.json()


def test_owner_can_manage_multiple_tenant_scoped_camera_sources() -> None:
    with build_client() as client:
        client.post("/v1/accounts", headers=USER_HEADER)
        first = client.post(
            "/v1/browser-cameras",
            headers=USER_HEADER,
            json={
                "name": "Phone by sink",
                "client_instance_id": "browser-instance-phone-0001",
            },
        )
        renamed = client.post(
            "/v1/browser-cameras",
            headers=USER_HEADER,
            json={
                "name": "Phone by air fryer",
                "client_instance_id": "browser-instance-phone-0001",
            },
        )
        second = client.post(
            "/v1/browser-cameras",
            headers=USER_HEADER,
            json={
                "name": "Tablet wide view",
                "client_instance_id": "browser-instance-tablet-0002",
            },
        )
        device = client.post(
            "/v1/device-cameras",
            headers=USER_HEADER,
            json={"name": "ESP kitchen camera"},
        )
        inventory = client.get("/v1/cameras", headers=USER_HEADER)
        revoked = client.post(
            f"/v1/cameras/{second.json()['id']}/revoke",
            headers=USER_HEADER,
        )

        foreign_headers = {"X-FoodLog-Local-User": "owner-b"}
        client.post("/v1/accounts", headers=foreign_headers)
        foreign_inventory = client.get("/v1/cameras", headers=foreign_headers)
        foreign_revoke = client.post(
            f"/v1/cameras/{first.json()['id']}/revoke",
            headers=foreign_headers,
        )
        inventory_after = client.get("/v1/cameras", headers=USER_HEADER)

    assert first.status_code == renamed.status_code == second.status_code == 200
    assert renamed.json()["id"] == first.json()["id"]
    assert renamed.json()["name"] == "Phone by air fryer"
    assert second.json()["id"] != first.json()["id"]
    assert device.status_code == 200
    assert inventory.status_code == 200
    assert {camera["id"] for camera in inventory.json()} == {
        first.json()["id"],
        second.json()["id"],
        device.json()["camera"]["id"],
    }
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert foreign_inventory.json() == []
    assert foreign_revoke.status_code == 404
    states = {camera["id"]: camera["status"] for camera in inventory_after.json()}
    assert states[first.json()["id"]] == "active"
    assert states[second.json()["id"]] == "revoked"


def capture_metadata(
    camera_id: str,
    image: bytes,
    *,
    client_kind: str = "browser",
) -> dict[str, object]:
    dimensions = image_dimensions(image, "image/png")
    assert dimensions is not None
    return {
        "schema_version": 1,
        "camera_id": camera_id,
        "captured_at": utc_now().isoformat(),
        "client_kind": client_kind,
        "client_version": "foodlog-test/0.1.0",
        "sequence_id": "test-sequence-0001",
        "sequence_number": 1,
        "width": dimensions[0],
        "height": dimensions[1],
    }


def shared_capture_request(
    *,
    headers: dict[str, str],
    metadata: dict[str, object],
    image: bytes,
) -> dict:
    return {
        "headers": headers,
        "data": {"metadata": json.dumps(metadata)},
        "files": {"image": ("capture.png", image, "image/png")},
    }


def post_shared_browser_capture(
    client: TestClient,
    *,
    camera: dict,
    image: bytes,
    idempotency_key: str,
    user: str = "owner-a",
    metadata: dict[str, object] | None = None,
):
    return client.post(
        "/v1/captures",
        **shared_capture_request(
            headers={
                "X-FoodLog-Local-User": user,
                "Idempotency-Key": idempotency_key,
            },
            metadata=metadata or capture_metadata(camera["id"], image),
            image=image,
        ),
    )


def post_fixture_capture(
    client: TestClient,
    *,
    camera: dict,
    image: bytes,
    idempotency_key: str,
    user: str = "owner-a",
    metadata: dict[str, object] | None = None,
):
    response = post_shared_browser_capture(
        client,
        camera=camera,
        image=image,
        idempotency_key=idempotency_key,
        user=user,
        metadata=metadata,
    )
    if response.status_code != 202 or response.json()["duplicate"]:
        return response

    async def seed_deterministic_result() -> None:
        repository = client.app.state.container.repository
        inference = await FixtureInferenceEngine().infer(image, "image/png")
        meal = await repository.save_meal(
            account_id=camera["account_id"],
            meal=MealEntry(
                **inference.model_dump(),
                id=str(uuid4()),
                account_id=camera["account_id"],
                capture_id=response.json()["capture_id"],
            ),
        )
        if inference.clarification_question and inference.clarification_reason:
            await repository.open_question(
                account_id=camera["account_id"],
                meal=meal,
                prompt=inference.clarification_question,
                reason=inference.clarification_reason,
            )
        await repository.mark_processed(
            account_id=camera["account_id"],
            capture_id=response.json()["capture_id"],
        )

    asyncio.run(seed_deterministic_result())
    return response


def test_shared_browser_ingestion_stores_metadata_and_bytes_without_inference() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
        metadata = capture_metadata(camera["id"], image)
        request = shared_capture_request(
            headers={**USER_HEADER, "Idempotency-Key": "shared-browser-capture-0001"},
            metadata=metadata,
            image=image,
        )
        accepted = client.post("/v1/captures", **request)
        retry = client.post("/v1/captures", **request)
        changed_metadata = {**metadata, "sequence_number": 2}
        conflict = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={
                    **USER_HEADER,
                    "Idempotency-Key": "shared-browser-capture-0001",
                },
                metadata=changed_metadata,
                image=image,
            ),
        )
        journal = client.get("/v1/journal", headers=USER_HEADER)
        stored_image = client.get(
            f"/v1/captures/{accepted.json()['capture_id']}/image",
            headers=USER_HEADER,
        )
        repository = client.app.state.container.repository
        stored_capture = repository._captures[accepted.json()["capture_id"]]

    assert accepted.status_code == retry.status_code == 202
    assert accepted.json()["duplicate"] is False
    assert retry.json()["duplicate"] is True
    assert retry.json()["accepted_image_count"] == 1
    assert conflict.status_code == 409
    assert journal.json() == []
    assert stored_image.content == image
    assert stored_capture.status == "stored"
    assert stored_capture.metadata == CaptureEnvelopeV1.model_validate(metadata)


def test_shared_ingestion_recovers_an_interrupted_reserved_capture() -> None:
    app = create_app(Settings(environment="test"))
    with TestClient(app) as client:
        account, camera = provision(client)
        image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
        metadata = capture_metadata(camera["id"], image)
        envelope = CaptureEnvelopeV1.model_validate(metadata)
        repository = client.app.state.container.repository
        stored_account = asyncio.run(repository.account_for_owner("owner-a"))
        stored_camera = asyncio.run(repository.camera_for_owner("owner-a", camera["id"]))
        interrupted_capture_id = "interrupted-reservation-capture"
        interrupted_object_key = f"accounts/{account['id']}/captures/{interrupted_capture_id}.png"
        reserved, _, created = asyncio.run(
            repository.reserve_capture(
                capture_id=interrupted_capture_id,
                account=stored_account,
                camera=stored_camera,
                idempotency_key="interrupted-reservation-0001",
                content_type="image/png",
                content_sha256=sha256(image).hexdigest(),
                object_key=interrupted_object_key,
                metadata=envelope,
            )
        )
        assert created is True
        assert reserved.status == "accepted"

        recovered = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={
                    **USER_HEADER,
                    "Idempotency-Key": "interrupted-reservation-0001",
                },
                metadata=metadata,
                image=image,
            ),
        )
        stored_image = client.get(
            f"/v1/captures/{interrupted_capture_id}/image",
            headers=USER_HEADER,
        )

    assert recovered.status_code == 202
    assert recovered.json()["capture_id"] == interrupted_capture_id
    assert recovered.json()["duplicate"] is True
    assert recovered.json()["accepted_image_count"] == 1
    assert stored_image.content == image
    assert repository._captures[interrupted_capture_id].status == "stored"


def test_shared_ingestion_retains_an_uploaded_object_for_finalize_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(Settings(environment="test"))
    with TestClient(app) as client:
        account, camera_response = provision(client)

    repository = app.state.container.repository
    object_store = app.state.container.object_store
    service = app.state.container.capture_service
    camera = asyncio.run(repository.camera_for_owner("owner-a", camera_response["id"]))
    image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
    metadata = CaptureEnvelopeV1.model_validate(capture_metadata(camera.id, image))
    original_mark_stored = repository.mark_stored
    finalize_attempts = 0

    async def fail_first_finalize(*, account_id: str, capture_id: str) -> None:
        nonlocal finalize_attempts
        finalize_attempts += 1
        if finalize_attempts == 1:
            raise RuntimeError("simulated Firestore finalization failure")
        await original_mark_stored(account_id=account_id, capture_id=capture_id)

    monkeypatch.setattr(repository, "mark_stored", fail_first_finalize)
    request = {
        "owner_user_id": "owner-a",
        "camera": camera,
        "idempotency_key": "finalize-retry-capture-0001",
        "content_type": "image/png",
        "image": image,
        "metadata": metadata,
    }

    with pytest.raises(RuntimeError, match="finalization failure"):
        asyncio.run(service.accept_capture(**request))

    captures = list(repository._captures.values())
    assert len(captures) == 1
    reserved = captures[0]
    assert reserved.status == "accepted"
    assert asyncio.run(object_store.get(reserved.object_key)) == image
    assert repository._accounts[account["id"]].accepted_image_count == 1

    recovered = asyncio.run(service.accept_capture(**request))

    assert recovered.capture_id == reserved.id
    assert recovered.duplicate is True
    assert recovered.accepted_image_count == 1
    assert repository._captures[reserved.id].status == "stored"
    assert asyncio.run(object_store.get(reserved.object_key)) == image


def test_shared_device_ingestion_uses_credential_scope_and_honors_revocation() -> None:
    owner_headers = {"X-FoodLog-Local-User": "device-capture-owner"}
    with build_client() as client:
        account = client.post("/v1/accounts", headers=owner_headers)
        assert account.status_code == 200
        issued = client.post(
            "/v1/device-cameras",
            headers=owner_headers,
            json={"name": "Physical kitchen camera"},
        )
        assert issued.status_code == 200
        camera = issued.json()["camera"]
        credential = issued.json()["credential"]
        image = (FIXTURES / "synthetic-chicken-airfryer.png").read_bytes()
        metadata = capture_metadata(camera["id"], image, client_kind="physical")
        accepted = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={
                    "Authorization": f"FoodLogCamera {credential}",
                    "Idempotency-Key": "shared-device-capture-0001",
                },
                metadata=metadata,
                image=image,
            ),
        )
        wrong_camera = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={
                    "Authorization": f"FoodLogCamera {credential}",
                    "Idempotency-Key": "shared-device-capture-0002",
                },
                metadata={**metadata, "camera_id": "different-camera"},
                image=image,
            ),
        )
        revoked = client.post(
            f"/v1/device-cameras/{camera['id']}/revoke",
            headers=owner_headers,
        )
        after_revoke = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={
                    "Authorization": f"FoodLogCamera {credential}",
                    "Idempotency-Key": "shared-device-capture-0003",
                },
                metadata=metadata,
                image=image,
            ),
        )
        stored_image = client.get(
            f"/v1/captures/{accepted.json()['capture_id']}/image",
            headers=owner_headers,
        )

    assert accepted.status_code == 202
    assert wrong_camera.status_code == 403
    assert wrong_camera.json() == {"detail": "camera_identity_mismatch"}
    assert revoked.status_code == 200
    assert after_revoke.status_code == 401
    assert stored_image.content == image


def test_shared_ingestion_rejects_invalid_metadata_dimensions_and_image_structure() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-leftover-pasta.png").read_bytes()
        metadata = capture_metadata(camera["id"], image)
        invalid_metadata = client.post(
            "/v1/captures",
            headers={**USER_HEADER, "Idempotency-Key": "invalid-metadata-0001"},
            data={"metadata": "{not-json"},
            files={"image": ("capture.png", image, "image/png")},
        )
        wrong_dimensions = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={**USER_HEADER, "Idempotency-Key": "wrong-dimensions-0001"},
                metadata={**metadata, "width": int(metadata["width"]) + 1},
                image=image,
            ),
        )
        truncated = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={**USER_HEADER, "Idempotency-Key": "truncated-image-0001"},
                metadata=metadata,
                image=b"\x89PNG\r\n\x1a\ntruncated",
            ),
        )
        wrong_kind = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={**USER_HEADER, "Idempotency-Key": "wrong-kind-0001"},
                metadata={**metadata, "client_kind": "physical"},
                image=image,
            ),
        )
        future_timestamp = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={**USER_HEADER, "Idempotency-Key": "future-time-0001"},
                metadata={**metadata, "captured_at": "2099-01-01T00:00:00Z"},
                image=image,
            ),
        )

    assert invalid_metadata.status_code == 422
    assert invalid_metadata.json() == {"detail": "invalid_capture_metadata"}
    assert wrong_dimensions.status_code == 422
    assert wrong_dimensions.json() == {"detail": "image_dimensions_mismatch"}
    assert truncated.status_code == 415
    assert truncated.json() == {"detail": "Image dimensions could not be read"}
    assert wrong_kind.status_code == 422
    assert wrong_kind.json() == {"detail": "camera_client_kind_mismatch"}
    assert future_timestamp.status_code == 422
    assert future_timestamp.json() == {"detail": "captured_at_too_far_in_future"}


def test_image_validation_fully_decodes_jpeg_and_rejects_truncation() -> None:
    buffer = BytesIO()
    Image.new("RGB", (3, 2), color=(120, 80, 40)).save(buffer, format="JPEG")
    jpeg = buffer.getvalue()

    assert image_dimensions(jpeg, "image/jpeg") == (3, 2)
    assert image_dimensions(jpeg[:-8], "image/jpeg") is None
    assert image_dimensions(jpeg, "image/png") is None


def test_fixture_capture_creates_explainable_journal_entry() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
        response = post_fixture_capture(
            client,
            camera=camera,
            image=image,
            idempotency_key="capture-steak-0001",
        )
        assert response.status_code == 202
        assert response.json()["accepted_image_count"] == 1

        journal = client.get("/v1/journal", headers=USER_HEADER)
        assert journal.status_code == 200
        entry = journal.json()[0]
        assert entry["title"] == "Air-fried steak"
        assert entry["confidence"] == "confident"
        assert entry["observations"]

        image_response = client.get(
            f"/v1/captures/{entry['capture_id']}/image",
            headers=USER_HEADER,
        )
        assert image_response.status_code == 200
        assert image_response.content == image
        assert image_response.headers["cache-control"] == "private, no-store"
        assert image_response.headers["content-disposition"] == "inline"
        assert image_response.headers["x-content-type-options"] == "nosniff"
        assert image_response.headers["content-type"] == "image/png"


def test_idempotent_retry_does_not_consume_quota_or_duplicate_meal() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-chicken-airfryer.png").read_bytes()
        metadata = capture_metadata(camera["id"], image)
        first = post_fixture_capture(
            client,
            camera=camera,
            image=image,
            idempotency_key="capture-chicken-0001",
            metadata=metadata,
        )
        retry = post_fixture_capture(
            client,
            camera=camera,
            image=image,
            idempotency_key="capture-chicken-0001",
            metadata=metadata,
        )
        assert first.status_code == retry.status_code == 202
        assert retry.json()["duplicate"] is True
        assert retry.json()["accepted_image_count"] == 1
        assert len(client.get("/v1/journal", headers=USER_HEADER).json()) == 1


def test_cross_account_camera_and_capture_access_fail_closed() -> None:
    with build_client() as client:
        _, camera_a = provision(client, "owner-a")
        _, _camera_b = provision(client, "owner-b")
        image = (FIXTURES / "synthetic-leftover-pasta.png").read_bytes()
        rejected = post_shared_browser_capture(
            client,
            camera=camera_a,
            image=image,
            idempotency_key="cross-account-0001",
            user="owner-b",
        )
        assert rejected.status_code == 404

        accepted = post_shared_browser_capture(
            client,
            camera=camera_a,
            image=image,
            idempotency_key="owner-capture-0001",
        )
        capture_id = accepted.json()["capture_id"]
        hidden = client.get(
            f"/v1/captures/{capture_id}/image",
            headers={"X-FoodLog-Local-User": "owner-b"},
        )
        assert hidden.status_code == 404


def test_unknown_image_is_explicitly_uncertain_without_model_call() -> None:
    with build_client() as client:
        _, camera = provision(client)
        one_pixel_png = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        response = post_fixture_capture(
            client,
            camera=camera,
            image=one_pixel_png,
            idempotency_key="unknown-capture-0001",
        )
        assert response.status_code == 202
        entry = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        assert entry["confidence"] == "uncertain"
        assert "never calls Gemini" in entry["rationale"]


def test_adversarial_camera_fixtures_remain_uncertain_without_model_call() -> None:
    fixture_paths = sorted(ADVERSARIAL_FIXTURES.glob("*.png"))
    assert len(fixture_paths) == 3

    for index, fixture_path in enumerate(fixture_paths):
        with build_client() as client:
            _, camera = provision(client)
            response = post_fixture_capture(
                client,
                camera=camera,
                image=fixture_path.read_bytes(),
                idempotency_key=f"adversarial-capture-{index:02d}",
            )

            assert response.status_code == 202
            entry = client.get("/v1/journal", headers=USER_HEADER).json()[0]
            assert entry["title"] == "Unrecognized kitchen activity"
            assert entry["confidence"] == "uncertain"
            questions = client.get("/v1/questions", headers=USER_HEADER).json()
            assert len(questions) == 1
            assert questions[0]["meal_id"] == entry["id"]


def test_declared_image_type_must_match_content() -> None:
    with build_client() as client:
        _, camera = provision(client)
        response = client.post(
            "/v1/captures",
            **shared_capture_request(
                headers={**USER_HEADER, "Idempotency-Key": "invalid-image-0001"},
                metadata={
                    "schema_version": 1,
                    "camera_id": camera["id"],
                    "captured_at": utc_now().isoformat(),
                    "client_kind": "browser",
                    "client_version": "foodlog-test/0.1.0",
                    "sequence_id": "test-sequence-0001",
                    "sequence_number": 1,
                    "width": 1,
                    "height": 1,
                },
                image=b"not-a-real-png",
            ),
        )
        assert response.status_code == 415


def test_obsolete_browser_capture_route_is_not_exposed() -> None:
    with build_client() as client:
        _, camera = provision(client)
        response = client.post(
            f"/v1/browser-cameras/{camera['id']}/captures",
            headers={**USER_HEADER, "Idempotency-Key": "obsolete-route-0001"},
        )

    assert response.status_code == 404


def test_fixture_directory_matches_registered_ground_truth() -> None:
    assert verify_fixture_files(FIXTURES) == []


def test_confirmation_is_idempotent_and_preserves_original_revision() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
        capture = post_fixture_capture(
            client,
            camera=camera,
            image=image,
            idempotency_key="confirm-capture-0001",
        )
        meal = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        request = {
            "headers": {**USER_HEADER, "Idempotency-Key": "confirm-feedback-0001"},
            "json": {"kind": "confirm"},
        }

        first = client.post(f"/v1/meals/{meal['id']}/feedback", **request)
        retry = client.post(f"/v1/meals/{meal['id']}/feedback", **request)

        assert capture.status_code == 202
        assert first.status_code == retry.status_code == 200
        assert first.json()["revision"]["id"] == retry.json()["revision"]["id"]
        current = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        assert current["status"] == "confirmed"
        assert current["revision_number"] == 2
        revisions = client.get(
            f"/v1/meals/{meal['id']}/revisions",
            headers=USER_HEADER,
        ).json()
        assert [revision["source"] for revision in revisions] == [
            "inference",
            "user_feedback",
        ]
        assert revisions[0]["inference"]["title"] == "Air-fried steak"


def test_correction_keeps_original_inference_and_supports_unresolved_feedback() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-chicken-airfryer.png").read_bytes()
        post_fixture_capture(
            client,
            camera=camera,
            image=image,
            idempotency_key="correct-capture-0001",
        )
        meal = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        corrected = client.post(
            f"/v1/meals/{meal['id']}/feedback",
            headers={**USER_HEADER, "Idempotency-Key": "correct-feedback-0001"},
            json={
                "kind": "correct",
                "actual_meal": "Turkey breast",
                "explanation": "The fillets came from the turkey package visible earlier.",
            },
        )
        unresolved = client.post(
            f"/v1/meals/{meal['id']}/feedback",
            headers={**USER_HEADER, "Idempotency-Key": "correct-feedback-0002"},
            json={
                "kind": "correct",
                "explanation": "That was not the final meal, but I do not know what replaced it.",
            },
        )

        assert corrected.status_code == unresolved.status_code == 200
        current = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        assert current["status"] == "contradicted"
        assert current["title"] == "Unresolved meal"
        assert current["revision_number"] == 3
        revisions = client.get(
            f"/v1/meals/{meal['id']}/revisions",
            headers=USER_HEADER,
        ).json()
        assert revisions[0]["inference"]["title"] == "Air-fried chicken breast"
        assert revisions[1]["inference"]["title"] == "Turkey breast"
        assert revisions[2]["status"] == "contradicted"


def test_raw_partial_corrections_are_exact_immutable_and_idempotent() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
        post_fixture_capture(
            client,
            camera=camera,
            image=image,
            idempotency_key="raw-feedback-capture-0001",
        )
        meal = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        cases = [
            ("raw-wrong-only-0001", {"kind": "correct"}, None, None),
            (
                "raw-actual-only-0001",
                {"kind": "correct", "actual_meal": "Sirloin steak"},
                "Sirloin steak",
                None,
            ),
            (
                "raw-explanation-only-0001",
                {"kind": "correct", "explanation": "The package had a green label."},
                None,
                "The package had a green label.",
            ),
            (
                "raw-complete-0001",
                {
                    "kind": "correct",
                    "actual_meal": "Ribeye steak",
                    "explanation": "The thick marbling distinguishes this cut.",
                },
                "Ribeye steak",
                "The thick marbling distinguishes this cut.",
            ),
        ]
        for key, payload, actual_meal, explanation in cases:
            request = {
                "headers": {**USER_HEADER, "Idempotency-Key": key},
                "json": payload,
            }
            first = client.post(f"/v1/meals/{meal['id']}/feedback", **request)
            retry = client.post(f"/v1/meals/{meal['id']}/feedback", **request)
            assert first.status_code == retry.status_code == 200
            assert first.json() == retry.json()
            assert first.json()["feedback"]["actual_meal"] == actual_meal
            assert first.json()["feedback"]["explanation"] == explanation

        conflict = client.post(
            f"/v1/meals/{meal['id']}/feedback",
            headers={**USER_HEADER, "Idempotency-Key": cases[-1][0]},
            json={"kind": "correct", "actual_meal": "A different value"},
        )
        assert conflict.status_code == 409
        assert conflict.json() == {
            "detail": "idempotency_key_reused_with_different_payload"
        }
        revisions = client.get(
            f"/v1/meals/{meal['id']}/revisions",
            headers=USER_HEADER,
        ).json()
        assert [revision["number"] for revision in revisions] == [1, 2, 3, 4, 5]


def test_uncertain_question_answer_revises_meal_and_closes_inbox() -> None:
    with build_client() as client:
        _, camera = provision(client)
        one_pixel_png = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        post_fixture_capture(
            client,
            camera=camera,
            image=one_pixel_png,
            idempotency_key="question-capture-0001",
        )
        questions = client.get("/v1/questions", headers=USER_HEADER).json()
        assert len(questions) == 1
        question = questions[0]
        request = {
            "headers": {**USER_HEADER, "Idempotency-Key": "question-answer-0001"},
            "json": {
                "answer": "Vegetable soup",
                "learning_tip": "The blue pot is normally used for soup.",
            },
        }

        first = client.post(f"/v1/questions/{question['id']}/answer", **request)
        retry = client.post(f"/v1/questions/{question['id']}/answer", **request)

        assert first.status_code == retry.status_code == 200
        assert first.json()["revision"]["id"] == retry.json()["revision"]["id"]
        assert first.json()["feedback"]["actual_meal"] == "Vegetable soup"
        assert first.json()["feedback"]["explanation"] == (
            "The blue pot is normally used for soup."
        )
        assert first.json()["feedback"]["question_id"] == question["id"]
        assert client.get("/v1/questions", headers=USER_HEADER).json() == []
        answered = client.get(
            "/v1/questions?question_status=answered",
            headers=USER_HEADER,
        ).json()
        assert answered[0]["answer"] == "Vegetable soup"
        current = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        assert current["title"] == "Vegetable soup"
        assert current["status"] == "corrected"


def test_feedback_and_question_access_are_tenant_scoped() -> None:
    with build_client() as client:
        _, camera = provision(client, "owner-a")
        provision(client, "owner-b")
        one_pixel_png = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        post_fixture_capture(
            client,
            camera=camera,
            image=one_pixel_png,
            idempotency_key="scoped-question-capture-0001",
        )
        meal = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        question = client.get("/v1/questions", headers=USER_HEADER).json()[0]
        owner_b = {"X-FoodLog-Local-User": "owner-b"}

        feedback = client.post(
            f"/v1/meals/{meal['id']}/feedback",
            headers={**owner_b, "Idempotency-Key": "cross-feedback-0001"},
            json={"kind": "confirm"},
        )
        answer = client.post(
            f"/v1/questions/{question['id']}/answer",
            headers={**owner_b, "Idempotency-Key": "cross-answer-0001"},
            json={"answer": "Not your meal"},
        )

        assert feedback.status_code == 404
        assert answer.status_code == 404
