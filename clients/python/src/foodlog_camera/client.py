from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx

RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class CameraClientError(RuntimeError):
    """A safe, credential-free camera client failure."""


@dataclass(frozen=True, slots=True)
class CaptureAccepted:
    capture_id: str
    accepted_image_count: int
    entitlement_mode: str
    trial_image_limit: int | None
    duplicate: bool


class FoodLogCameraClient:
    def __init__(
        self,
        *,
        api_base: str,
        camera_id: str,
        credential: str,
        client_version: str = "python-simulator/1",
        max_attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_base = validate_api_base(api_base)
        self.camera_id = camera_id
        self._credential = validate_credential(credential)
        self.client_version = client_version
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self._api_base,
            headers={"Authorization": f"FoodLogCamera {self._credential}"},
            timeout=httpx.Timeout(30),
            transport=transport,
        )

    def __enter__(self) -> FoodLogCameraClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"FoodLogCameraClient(api_base={self._api_base!r}, "
            f"camera_id={self.camera_id!r}, credential=<redacted>)"
        )

    def close(self) -> None:
        self._http.close()

    def status(self) -> dict[str, str]:
        response = self._request("GET", "/v1/device/status")
        payload = response.json()
        if payload.get("camera_id") != self.camera_id or payload.get("status") != "active":
            raise CameraClientError("The API returned an invalid camera status response.")
        return {"camera_id": payload["camera_id"], "status": payload["status"]}

    def upload_capture(
        self,
        *,
        image: bytes,
        content_type: str,
        width: int,
        height: int,
        captured_at: datetime,
        sequence_id: str,
        sequence_number: int,
        idempotency_key: str | None = None,
        burst_id: str | None = None,
        burst_frame_index: int | None = None,
    ) -> CaptureAccepted:
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at must include a UTC offset")
        if not image:
            raise ValueError("image must not be empty")
        if content_type not in {"image/jpeg", "image/png"}:
            raise ValueError("content_type must be image/jpeg or image/png")

        metadata: dict[str, object] = {
            "schema_version": 1,
            "camera_id": self.camera_id,
            "captured_at": captured_at.isoformat(),
            "client_kind": "simulator",
            "client_version": self.client_version,
            "sequence_id": sequence_id,
            "sequence_number": sequence_number,
            "width": width,
            "height": height,
        }
        if (burst_id is None) != (burst_frame_index is None):
            raise ValueError("burst_id and burst_frame_index must be supplied together")
        if burst_id is not None:
            metadata["burst_id"] = burst_id
            metadata["burst_frame_index"] = burst_frame_index

        key = idempotency_key or f"python-{uuid4()}"
        extension = ".jpg" if content_type == "image/jpeg" else ".png"
        response = self._request(
            "POST",
            "/v1/captures",
            headers={"Idempotency-Key": key},
            files={
                "metadata": (None, json.dumps(metadata, separators=(",", ":")), "application/json"),
                "image": (f"capture{extension}", image, content_type),
            },
        )
        payload = response.json()
        try:
            return CaptureAccepted(
                capture_id=payload["capture_id"],
                accepted_image_count=payload["accepted_image_count"],
                entitlement_mode=payload["entitlement_mode"],
                trial_image_limit=payload["trial_image_limit"],
                duplicate=payload["duplicate"],
            )
        except (KeyError, TypeError) as error:
            raise CameraClientError("The API returned an invalid capture response.") from error

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as error:
                if attempt == self.max_attempts:
                    raise CameraClientError("The FoodLog API could not be reached.") from error
                self._sleep(2 ** (attempt - 1))
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_attempts:
                self._sleep(2 ** (attempt - 1))
                continue
            if response.is_success:
                return response
            if response.status_code in {401, 403}:
                raise CameraClientError("The camera credential is invalid or revoked.")
            if response.status_code == 429:
                raise CameraClientError("The image quota or API rate limit has been reached.")
            raise CameraClientError(
                f"The FoodLog API rejected the request ({response.status_code})."
            )

        raise AssertionError("request retry loop ended unexpectedly")


def validate_credential(value: str) -> str:
    credential = value.strip()
    if not credential.startswith("flc_v1_") or len(credential) > 256 or " " in credential:
        raise ValueError("camera credential has an invalid format")
    return credential


def validate_api_base(value: str) -> str:
    api_base = value.rstrip("/")
    parsed = urlparse(api_base)
    if parsed.scheme == "https" and parsed.netloc:
        return api_base
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}:
        return api_base
    raise ValueError("api_base must use HTTPS, except for a loopback development server")


def read_credential_file(path: Path) -> str:
    return validate_credential(path.read_text(encoding="utf-8"))
