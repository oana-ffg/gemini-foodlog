"""Prove one real encrypted microSD capture survives an ESP32 reboot."""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import requests
import serial

API_URL = "https://foodlog-api-sptvo5nsga-ew.a.run.app"
ORIGIN = "https://gemini-foodlog-2026.web.app"
FIREBASE_SIGN_IN = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)


class DataBlob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_unprotect(path: Path) -> dict[str, object]:
    encrypted = bytearray(path.read_bytes())
    input_buffer = (ctypes.c_byte * len(encrypted)).from_buffer(encrypted)
    input_blob = DataBlob(len(encrypted), input_buffer)
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError()
    plaintext = bytearray()
    try:
        plaintext.extend(ctypes.string_at(output_blob.data, output_blob.length))
        document = json.loads(plaintext.decode("utf-8"))
        if not isinstance(document, dict):
            raise TypeError("protected camera configuration must be a JSON object")
        return document
    finally:
        if plaintext:
            ctypes.memset((ctypes.c_byte * len(plaintext)).from_buffer(plaintext), 0, len(plaintext))
        ctypes.memset(input_buffer, 0, len(encrypted))
        kernel32.LocalFree(output_blob.data)


def _required_string(document: dict[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"protected configuration is missing {name}")
    return value


def _owner_client(account: dict[str, object], firebase_api_key: str) -> requests.Session:
    response = requests.post(
        FIREBASE_SIGN_IN,
        params={"key": firebase_api_key},
        headers={"Origin": ORIGIN, "Referer": f"{ORIGIN}/"},
        json={
            "email": _required_string(account, "email"),
            "password": _required_string(account, "password"),
            "returnSecureToken": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("idToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Firebase sign-in returned no ID token")
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
        }
    )
    return session


def _reset_to_normal_boot(port: serial.Serial, esptool: Path) -> None:
    # Keep GPIO0 released while pulsing EN through the standard ESP32
    # auto-reset circuit. This interrupts the committed queue record without
    # entering the ROM downloader.
    port.dtr = False
    port.rts = True
    time.sleep(0.15)
    port.rts = False
    port.close()
    result = subprocess.run(
        [
            sys.executable,
            str(esptool),
            "--chip",
            "esp32s3",
            "--port",
            port.port,
            "--baud",
            "115200",
            "--before",
            "default_reset",
            "--after",
            "hard_reset",
            "chip_id",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("standard ESP32 hard reset failed")
    port.open()
    port.dtr = False
    port.rts = False
    port.reset_input_buffer()


def _readline(port: serial.Serial, deadline: float) -> str:
    while time.monotonic() < deadline:
        raw = port.readline()
        if raw:
            return raw.decode("utf-8", errors="replace").strip()
    raise TimeoutError("camera serial evidence timed out")


def run(args: argparse.Namespace) -> None:
    account = _dpapi_unprotect(args.account_dpapi.resolve())
    device = _dpapi_unprotect(args.device_dpapi.resolve())
    camera_id = _required_string(device, "camera_id")

    with _owner_client(account, args.firebase_api_key) as client, serial.Serial(
        args.port, 115_200, timeout=0.2
    ) as camera:
        camera.reset_input_buffer()
        boot_deadline = time.monotonic() + 45
        while True:
            line = _readline(camera, boot_deadline)
            if "API_STATUS status=ready" in line:
                break

        request_response = client.post(
            f"{API_URL}/v1/device-cameras/{camera_id}/snapshot-requests",
            timeout=30,
        )
        if request_response.status_code != 202:
            raise RuntimeError(
                f"snapshot request returned HTTP {request_response.status_code}"
            )
        request_id = _required_string(request_response.json(), "id")

        queued_before_reset = False
        recovered_after_reset = False
        accepted_after_reset = False
        deadline = time.monotonic() + args.timeout_seconds
        while time.monotonic() < deadline and not accepted_after_reset:
            line = _readline(camera, deadline)
            if not queued_before_reset and "CAPTURE_QUEUED source=manual" in line:
                queued_before_reset = True
                _reset_to_normal_boot(camera, args.esptool.resolve())
                continue
            if queued_before_reset and "QUEUE_STORAGE status=ready" in line:
                recovered_after_reset = "queued=1" in line
            if recovered_after_reset and "CAPTURE_ACCEPTED" in line and "source=manual" in line:
                accepted_after_reset = "queued=0" in line

        if not queued_before_reset:
            raise AssertionError("manual capture was not committed before reset")
        if not recovered_after_reset:
            raise AssertionError("encrypted queue did not recover one item after reset")
        if not accepted_after_reset:
            raise AssertionError("recovered capture was not accepted and removed")

        status = client.get(
            f"{API_URL}/v1/device-cameras/{camera_id}/snapshot-requests/{request_id}",
            timeout=30,
        )
        status.raise_for_status()
        request = status.json()
        if request.get("status") != "completed":
            raise AssertionError("owner request did not complete after queue recovery")
        capture_id = _required_string(request, "capture_id")

    print("queue_commit_before_reset=true")
    print("queue_recovered_after_reset=true")
    print("queue_removed_after_acknowledgement=true")
    print(f"snapshot_request_id={request_id}")
    print(f"capture_id={capture_id}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM4")
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument(
        "--account-dpapi",
        type=Path,
        default=Path(".foodlog/secrets/camera-test-account.json.dpapi"),
    )
    parser.add_argument(
        "--device-dpapi",
        type=Path,
        default=Path(".foodlog/secrets/camera-device-config.json.dpapi"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument(
        "--esptool",
        type=Path,
        default=Path.home() / ".platformio/packages/tool-esptoolpy/esptool.py",
    )
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
