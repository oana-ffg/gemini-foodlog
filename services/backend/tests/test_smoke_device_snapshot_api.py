from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from scripts.smoke_device_snapshot_api import (
    EXPECTED_API_URL,
    EXPECTED_ORIGIN,
    print_evidence,
    run_smoke,
)

API_URL = EXPECTED_API_URL
ORIGIN = EXPECTED_ORIGIN
CAMERA_ID = "e5fe8f7d-e290-4942-891c-2607ca25c570"
REQUEST_ID = "95b60c03-6809-4780-8cf2-01cdb830058f"
CAPTURE_ID = "e6aa861f-e8ba-40f4-a773-2c2dc4dc5e22"
IMAGE = b"\xff\xd8private-camera-smoke\xff\xd9"


def args(output: Path, *, timeout_seconds: int = 120) -> argparse.Namespace:
    return argparse.Namespace(
        api_url=API_URL,
        firebase_api_key="firebase-test-key",
        origin=ORIGIN,
        camera_id=CAMERA_ID,
        output=output,
        timeout_seconds=timeout_seconds,
    )


def json_response(
    request: httpx.Request,
    status_code: int,
    document: object,
    *,
    private: bool = False,
) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if private:
        headers["Cache-Control"] = "private, no-store"
    return httpx.Response(
        status_code,
        request=request,
        headers=headers,
        content=json.dumps(document).encode(),
    )


def test_smoke_requests_owner_snapshot_and_saves_only_private_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("FOODLOG_SMOKE_EMAIL", "owner@example.test")
    monkeypatch.setenv("FOODLOG_SMOKE_PASSWORD", "correct horse battery staple")
    status_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_reads
        if request.url.host == "identitytoolkit.googleapis.com":
            assert request.headers["origin"] == ORIGIN
            body = json.loads(request.content)
            assert body["email"] == "owner@example.test"
            assert body["password"] == "correct horse battery staple"
            return json_response(request, 200, {"idToken": "private-id-token"})
        assert request.headers["authorization"] == "Bearer private-id-token"
        if request.url.path == "/v1/cameras":
            return json_response(
                request,
                200,
                [{"id": CAMERA_ID, "kind": "device", "status": "active"}],
            )
        if request.method == "POST" and request.url.path.endswith("/snapshot-requests"):
            return json_response(
                request,
                202,
                {"id": REQUEST_ID, "status": "pending"},
                private=True,
            )
        if request.url.path.endswith(f"/snapshot-requests/{REQUEST_ID}"):
            status_reads += 1
            return json_response(
                request,
                200,
                {
                    "id": REQUEST_ID,
                    "status": "completed",
                    "capture_id": CAPTURE_ID,
                },
                private=True,
            )
        if request.url.path == f"/v1/captures/{CAPTURE_ID}/image":
            return httpx.Response(
                200,
                request=request,
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Type": "image/jpeg",
                },
                content=IMAGE,
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    output = tmp_path / ".foodlog" / "camera-smoke.jpg"
    evidence = run_smoke(
        args(output),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    print_evidence(evidence)
    printed = capsys.readouterr().out

    assert output.read_bytes() == IMAGE
    assert evidence.request_id == REQUEST_ID
    assert evidence.capture_id == CAPTURE_ID
    assert evidence.image_bytes == len(IMAGE)
    assert status_reads == 1
    assert "owner@example.test" not in printed
    assert "correct horse battery staple" not in printed
    assert "private-id-token" not in printed
    assert "owner_authenticated=true" in printed
    assert "private_no_store=true" in printed


def test_smoke_refuses_non_private_or_existing_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"private \.foodlog"):
        run_smoke(args(tmp_path / "capture.jpg"))

    output = tmp_path / ".foodlog" / "capture.jpg"
    output.parent.mkdir()
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        run_smoke(args(output))


def test_smoke_refuses_to_send_owner_token_to_another_origin(tmp_path: Path) -> None:
    request_args = args(tmp_path / ".foodlog" / "capture.jpg")
    request_args.api_url = "https://not-foodlog.example.test"
    with pytest.raises(ValueError, match="locked to the FoodLog production origins"):
        run_smoke(request_args)


def test_smoke_times_out_without_writing_partial_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOODLOG_SMOKE_EMAIL", "owner@example.test")
    monkeypatch.setenv("FOODLOG_SMOKE_PASSWORD", "password")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "identitytoolkit.googleapis.com":
            return json_response(request, 200, {"idToken": "private-id-token"})
        if request.url.path == "/v1/cameras":
            return json_response(
                request,
                200,
                [{"id": CAMERA_ID, "kind": "device", "status": "active"}],
            )
        if request.method == "POST":
            return json_response(
                request,
                202,
                {"id": REQUEST_ID, "status": "pending"},
                private=True,
            )
        return json_response(
            request,
            200,
            {"id": REQUEST_ID, "status": "pending"},
            private=True,
        )

    clock_values: Iterator[float] = iter([0.0, 11.0])
    output = tmp_path / ".foodlog" / "timeout.jpg"
    with pytest.raises(TimeoutError, match="did not complete"):
        run_smoke(
            args(output, timeout_seconds=10),
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
            monotonic=lambda: next(clock_values),
        )
    assert not output.exists()
