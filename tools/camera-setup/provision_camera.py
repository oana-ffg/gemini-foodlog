"""Local serial provisioning for the FoodLog Freenove FNK0085 camera."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

EXPECTED_API_ORIGIN = "https://foodlog-api-sptvo5nsga-ew.a.run.app"
CAMERA_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class CameraSetup:
    camera_id: str
    credential: str


class SerialPort(Protocol):
    def write(self, data: bytes) -> int: ...

    def flush(self) -> None: ...

    def readline(self) -> bytes: ...

    def close(self) -> None: ...


def load_camera_setup(path: Path) -> CameraSetup:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read a valid FoodLog setup file: {error}") from error
    return camera_setup_from_document(document)


def camera_setup_from_document(document: object) -> CameraSetup:
    if not isinstance(document, dict):
        raise TypeError("FoodLog setup file must contain a JSON object")

    api_origin = document.get("api_base_url")
    if api_origin != EXPECTED_API_ORIGIN:
        raise ValueError("FoodLog setup file targets an unexpected API origin")
    parsed_origin = urlparse(api_origin)
    if parsed_origin.scheme != "https" or parsed_origin.path not in {"", "/"}:
        raise ValueError("FoodLog API origin must use HTTPS without a path")

    camera_id = document.get("camera_id")
    authorization = document.get("authorization")
    if not isinstance(camera_id, str) or not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise ValueError("FoodLog setup file has an invalid camera ID")
    if not isinstance(authorization, str) or not authorization.startswith(
        "FoodLogCamera flc_v1_"
    ):
        raise ValueError("FoodLog setup file has an invalid camera credential")
    credential = authorization.removeprefix("FoodLogCamera ")
    if len(credential) > 256 or any(character.isspace() for character in credential):
        raise ValueError("FoodLog camera credential is malformed")
    return CameraSetup(camera_id=camera_id, credential=credential)


def encoded_field(name: str, value: str) -> bytes:
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"{name} {encoded}\n".encode("ascii")


def wait_for_line(
    serial_port: SerialPort,
    expected_prefix: str,
    *,
    deadline_seconds: float = 15.0,
) -> str:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        raw_line = serial_port.readline()
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace").strip()
        if line.startswith("PROVISION_ERROR"):
            raise RuntimeError(line)
        if line.startswith(expected_prefix):
            return line
    raise TimeoutError(f"Camera did not answer with {expected_prefix}")


def send_line(serial_port: SerialPort, line: bytes, expected_prefix: str) -> str:
    serial_port.write(line)
    serial_port.flush()
    return wait_for_line(serial_port, expected_prefix)


def provision(
    serial_port: SerialPort,
    setup: CameraSetup,
    wifi_ssid: str,
    wifi_password: str,
) -> None:
    if not wifi_ssid or len(wifi_ssid.encode("utf-8")) > 32:
        raise ValueError("Wi-Fi name must be between 1 and 32 UTF-8 bytes")
    if len(wifi_password.encode("utf-8")) > 63:
        raise ValueError("Wi-Fi password must be at most 63 UTF-8 bytes")

    send_line(serial_port, b"PROVISION_BEGIN\n", "PROVISION_ACCEPTED")
    secret_fields = (
        ("WIFI_SSID", wifi_ssid),
        ("WIFI_PASSWORD", wifi_password),
        ("CAMERA_ID", setup.camera_id),
        ("CAMERA_CREDENTIAL", setup.credential),
    )
    for name, value in secret_fields:
        response = send_line(
            serial_port,
            encoded_field(name, value),
            "PROVISION_FIELD_OK",
        )
        if response != f"PROVISION_FIELD_OK name={name}":
            raise RuntimeError(f"Camera acknowledged the wrong field while sending {name}")
    response = send_line(serial_port, b"PROVISION_COMMIT\n", "PROVISION_OK")
    if f"camera_id={setup.camera_id}" not in response:
        raise RuntimeError("Camera committed an unexpected camera ID")


def available_ports() -> list[dict[str, str]]:
    try:
        from serial.tools import list_ports
    except ImportError as error:
        raise RuntimeError("pyserial is required; run the setup PowerShell script") from error
    return [
        {
            "device": port.device,
            "description": port.description or "Serial port",
            "hardware_id": port.hwid or "unknown",
        }
        for port in list_ports.comports()
    ]


def open_serial_port(port: str) -> SerialPort:
    try:
        import serial
    except ImportError as error:
        raise RuntimeError("pyserial is required; run the setup PowerShell script") from error
    return serial.Serial(port=port, baudrate=115_200, timeout=0.25)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port")
    parser.add_argument("--list-ports-json", action="store_true")
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument(
        "--wifi-json-stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--setup-json-stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def read_wifi_from_stdin() -> tuple[str, str]:
    try:
        document = json.load(sys.stdin)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Could not read Wi-Fi input from stdin") from error
    if not isinstance(document, dict):
        raise TypeError("Wi-Fi input must be a JSON object")
    wifi_ssid = document.get("wifi_ssid")
    wifi_password = document.get("wifi_password")
    if not isinstance(wifi_ssid, str) or not isinstance(wifi_password, str):
        raise TypeError("Wi-Fi input must contain string SSID and password fields")
    return wifi_ssid, wifi_password


def read_setup_and_wifi_from_stdin() -> tuple[CameraSetup, str, str]:
    try:
        document = json.load(sys.stdin)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Could not read camera setup from stdin") from error
    setup = camera_setup_from_document(document)
    if not isinstance(document, dict):
        raise TypeError("Camera setup input must be a JSON object")
    wifi_ssid = document.get("wifi_ssid")
    wifi_password = document.get("wifi_password")
    if not isinstance(wifi_ssid, str) or not isinstance(wifi_password, str):
        raise TypeError("Camera setup input must contain Wi-Fi strings")
    return setup, wifi_ssid, wifi_password


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.list_ports_json:
        print(json.dumps(available_ports()))
        return 0
    if args.validate_config:
        if args.config is None:
            raise ValueError("--config is required with --validate-config")
        setup = load_camera_setup(args.config.resolve())
        print(f"Validated FoodLog setup for camera {setup.camera_id}.")
        return 0
    if (args.config is None and not args.setup_json_stdin) or not args.port:
        raise ValueError("--config and --port are required for provisioning")

    if args.setup_json_stdin:
        setup, wifi_ssid, wifi_password = read_setup_and_wifi_from_stdin()
    else:
        setup = load_camera_setup(args.config.resolve())
    if args.wifi_json_stdin and not args.setup_json_stdin:
        wifi_ssid, wifi_password = read_wifi_from_stdin()
    elif not args.setup_json_stdin:
        wifi_ssid = input("Wi-Fi name (SSID): ").strip()
        wifi_password = getpass.getpass("Wi-Fi password (input is hidden): ")
    serial_port = open_serial_port(args.port)
    try:
        print("Provisioning locally over USB. Secret values will not be printed.")
        # Opening an ESP32-S3 serial port can reset the board. Let its USB/serial
        # task finish booting before sending the first provisioning command.
        time.sleep(2.0)
        provision(serial_port, setup, wifi_ssid, wifi_password)
    finally:
        serial_port.close()
    print(f"Provisioning succeeded for camera {setup.camera_id}. The camera is rebooting.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, TimeoutError, TypeError, ValueError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
