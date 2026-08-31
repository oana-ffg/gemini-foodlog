from __future__ import annotations

import argparse
import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

POLL_INTERVAL_SECONDS = 2.0
EXPECTED_API_URL = "https://foodlog-api-sptvo5nsga-ew.a.run.app"
EXPECTED_ORIGIN = "https://gemini-foodlog-2026.web.app"


@dataclass(frozen=True)
class SnapshotEvidence:
    request_id: str
    capture_id: str
    image_sha256: str
    image_bytes: int
    output_path: Path


def _private_output_path(path: Path) -> Path:
    resolved = path.resolve()
    if ".foodlog" not in {part.casefold() for part in resolved.parts}:
        raise ValueError("snapshot output must be inside a private .foodlog directory")
    if resolved.exists():
        raise FileExistsError(f"refusing to overwrite existing private artifact: {resolved}")
    return resolved


def _require_status(response: httpx.Response, expected: int, operation: str) -> None:
    if response.status_code != expected:
        raise RuntimeError(f"{operation} returned HTTP {response.status_code}")


def run_smoke(
    args: argparse.Namespace,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SnapshotEvidence:
    output_path = _private_output_path(args.output)
    if args.api_url != EXPECTED_API_URL or args.origin != EXPECTED_ORIGIN:
        raise ValueError("live snapshot smoke is locked to the FoodLog production origins")
    try:
        email = os.environ["FOODLOG_SMOKE_EMAIL"]
        password = os.environ["FOODLOG_SMOKE_PASSWORD"]
    except KeyError as error:
        raise RuntimeError(f"missing required environment variable: {error.args[0]}") from error

    with httpx.Client(transport=transport, timeout=30) as auth_client:
        auth_response = auth_client.post(
            "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
            params={"key": args.firebase_api_key},
            headers={"Origin": args.origin, "Referer": f"{args.origin}/"},
            json={
                "email": email,
                "password": password,
                "returnSecureToken": True,
            },
        )
    _require_status(auth_response, 200, "Firebase sign-in")
    token = auth_response.json().get("idToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Firebase sign-in returned no ID token")

    with httpx.Client(
        base_url=args.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        transport=transport,
        timeout=30,
    ) as client:
        cameras_response = client.get("/v1/cameras")
        _require_status(cameras_response, 200, "camera inventory")
        matching_cameras = [
            camera
            for camera in cameras_response.json()
            if camera.get("id") == args.camera_id
            and camera.get("kind") == "device"
            and camera.get("status") == "active"
        ]
        if len(matching_cameras) != 1:
            raise RuntimeError("the requested active physical camera is not owned by this account")

        request_response = client.post(
            f"/v1/device-cameras/{args.camera_id}/snapshot-requests"
        )
        _require_status(request_response, 202, "snapshot request")
        if request_response.headers.get("cache-control") != "private, no-store":
            raise RuntimeError("snapshot request response is missing private no-store caching")
        request = request_response.json()
        request_id = request.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise RuntimeError("snapshot request returned no request ID")

        deadline = monotonic() + args.timeout_seconds
        while request.get("status") == "pending":
            if monotonic() >= deadline:
                raise TimeoutError("camera did not complete the snapshot before the smoke timeout")
            sleep(POLL_INTERVAL_SECONDS)
            status_response = client.get(
                f"/v1/device-cameras/{args.camera_id}/snapshot-requests/{request_id}"
            )
            _require_status(status_response, 200, "snapshot status")
            if status_response.headers.get("cache-control") != "private, no-store":
                raise RuntimeError("snapshot status response is missing private no-store caching")
            request = status_response.json()

        if request.get("status") != "completed":
            raise RuntimeError(f"snapshot request ended with status {request.get('status')!r}")
        capture_id = request.get("capture_id")
        if not isinstance(capture_id, str) or not capture_id:
            raise RuntimeError("completed snapshot request returned no capture ID")

        image_response = client.get(f"/v1/captures/{capture_id}/image")
        _require_status(image_response, 200, "private capture image")
        if image_response.headers.get("cache-control") != "private, no-store":
            raise RuntimeError("private image response is missing private no-store caching")
        if not image_response.headers.get("content-type", "").startswith("image/"):
            raise RuntimeError("private capture response is not an image")
        image = image_response.content
        if not image:
            raise RuntimeError("private capture image is empty")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image)
    return SnapshotEvidence(
        request_id=request_id,
        capture_id=capture_id,
        image_sha256=hashlib.sha256(image).hexdigest(),
        image_bytes=len(image),
        output_path=output_path,
    )


def _bounded_timeout(value: str) -> int:
    timeout = int(value)
    if not 10 <= timeout <= 300:
        raise argparse.ArgumentTypeError("timeout must be between 10 and 300 seconds")
    return timeout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Request and privately save one live physical-camera snapshot."
    )
    parser.add_argument("--api-url", default=EXPECTED_API_URL, choices=[EXPECTED_API_URL])
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", default=EXPECTED_ORIGIN, choices=[EXPECTED_ORIGIN])
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=_bounded_timeout, default=120)
    return parser.parse_args()


def print_evidence(evidence: SnapshotEvidence) -> None:
    print(f"snapshot_request_id={evidence.request_id}")
    print(f"capture_id={evidence.capture_id}")
    print(f"image_sha256={evidence.image_sha256}")
    print(f"image_bytes={evidence.image_bytes}")
    print(f"private_output={evidence.output_path}")
    print("owner_authenticated=true")
    print("private_no_store=true")
    print("model_calls=0")


if __name__ == "__main__":
    print_evidence(run_smoke(parse_args()))
