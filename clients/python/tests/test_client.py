from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from foodlog_camera.client import CameraClientError, FoodLogCameraClient, MotionMetadata

CAMERA_ID = "camera-test-1"
CREDENTIAL = "flc_v1_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"


def test_status_uses_device_scheme_and_redacts_credential() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"FoodLogCamera {CREDENTIAL}"
        return httpx.Response(200, json={"camera_id": CAMERA_ID, "status": "active"})

    with camera_client(handler) as client:
        assert client.status() == {"camera_id": CAMERA_ID, "status": "active"}
        assert CREDENTIAL not in repr(client)
        assert "<redacted>" in repr(client)


def test_upload_retries_with_same_idempotency_and_metadata() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(202, json={
            "capture_id": "capture-1",
            "accepted_image_count": 7,
            "entitlement_mode": "trial",
            "trial_image_limit": 200,
            "duplicate": False,
        })

    with camera_client(handler, sleep=sleeps.append) as client:
        accepted = client.upload_capture(
            image=b"\x89PNG\r\n\x1a\nbytes",
            content_type="image/png",
            width=10,
            height=20,
            captured_at=datetime(2026, 8, 25, 17, 0, tzinfo=UTC),
            sequence_id="sequence-1",
            sequence_number=3,
            idempotency_key="idempotency-1",
        )

    assert accepted.capture_id == "capture-1"
    assert accepted.accepted_image_count == 7
    assert sleeps == [1]
    assert len(requests) == 2
    assert {request.headers["Idempotency-Key"] for request in requests} == {"idempotency-1"}
    for body in (request.content for request in requests):
        assert b'"camera_id":"camera-test-1"' in body
        assert b'"client_kind":"simulator"' in body
        assert b"\x89PNG\r\n\x1a\nbytes" in body


def test_revoked_credential_fails_without_leaking_secret() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "A valid camera credential is required"})

    with (
        camera_client(handler) as client,
        pytest.raises(CameraClientError, match="invalid or revoked") as raised,
    ):
        client.status()
    assert CREDENTIAL not in str(raised.value)


def test_upload_includes_bounded_motion_burst_metadata() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={
            "capture_id": "capture-motion-1",
            "accepted_image_count": 8,
            "entitlement_mode": "trial",
            "trial_image_limit": 200,
            "duplicate": False,
        })

    with camera_client(handler) as client:
        client.upload_capture(
            image=b"\x89PNG\r\n\x1a\nbytes",
            content_type="image/png",
            width=10,
            height=20,
            captured_at=datetime(2026, 8, 25, 17, 0, tzinfo=UTC),
            sequence_id="sequence-motion-1",
            sequence_number=4,
            burst_id="motion-burst-1",
            burst_frame_index=2,
            motion=MotionMetadata(
                detected=True,
                algorithm="browser-luma-delta-v1",
                score=0.24,
                changed_pixel_ratio=0.18,
                threshold=0.03,
            ),
        )

    body = requests[0].content
    assert b'"burst_id":"motion-burst-1"' in body
    assert b'"burst_frame_index":2' in body
    assert b'"algorithm":"browser-luma-delta-v1"' in body
    assert b'"changed_pixel_ratio":0.18' in body


def test_motion_metadata_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="between zero and one"):
        MotionMetadata(detected=True, algorithm="test", score=1.1)


def camera_client(
    handler,
    *,
    sleep=lambda _: None,
) -> FoodLogCameraClient:
    return FoodLogCameraClient(
        api_base="https://foodlog.test",
        camera_id=CAMERA_ID,
        credential=CREDENTIAL,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )
