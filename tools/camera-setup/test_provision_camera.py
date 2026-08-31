from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from provision_camera import (
    CameraSetup,
    encoded_field,
    load_camera_setup,
    provision,
    read_setup_and_wifi_from_stdin,
    read_wifi_from_stdin,
)


class FakeSerial:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.responses: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        line = data.decode("ascii").strip()
        if line == "PROVISION_BEGIN":
            self.responses.append(b"PROVISION_ACCEPTED\n")
        elif line == "PROVISION_COMMIT":
            self.responses.append(
                b"PROVISION_OK camera_id=12345678-1234-1234-1234-123456789abc\n"
            )
        else:
            self.responses.append(
                f"PROVISION_FIELD_OK name={line.split(' ', 1)[0]}\n".encode()
            )
        return len(data)

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        return self.responses.pop(0) if self.responses else b""

    def close(self) -> None:
        return None


class ProvisionCameraTests(unittest.TestCase):
    def test_loads_only_the_production_foodlog_setup_shape(self) -> None:
        document = {
            "api_base_url": "https://foodlog-api-sptvo5nsga-ew.a.run.app",
            "camera_id": "12345678-1234-1234-1234-123456789abc",
            "authorization": "FoodLogCamera flc_v1_secret-value",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            setup = load_camera_setup(path)
        self.assertEqual(setup.camera_id, document["camera_id"])
        self.assertEqual(setup.credential, "flc_v1_secret-value")

    def test_rejects_a_setup_file_for_another_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.json"
            path.write_text(
                json.dumps(
                    {
                        "api_base_url": "https://attacker.example",
                        "camera_id": "12345678-1234-1234-1234-123456789abc",
                        "authorization": "FoodLogCamera flc_v1_secret-value",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unexpected API origin"):
                load_camera_setup(path)

    def test_serial_protocol_never_sends_plaintext_secrets(self) -> None:
        serial_port = FakeSerial()
        setup = CameraSetup(
            camera_id="12345678-1234-1234-1234-123456789abc",
            credential="flc_v1_camera-secret",
        )
        provision(serial_port, setup, "Kitchen WiFi", "wifi-password")
        transcript = b"".join(serial_port.writes)
        self.assertNotIn(b"wifi-password", transcript)
        self.assertNotIn(b"flc_v1_camera-secret", transcript)
        self.assertIn(encoded_field("CAMERA_ID", setup.camera_id), serial_port.writes)

    def test_reads_noninteractive_wifi_input_without_logging_it(self) -> None:
        original_stdin = __import__("sys").stdin
        try:
            __import__("sys").stdin = io.StringIO(
                json.dumps({"wifi_ssid": "Kitchen WiFi", "wifi_password": "secret"})
            )
            self.assertEqual(read_wifi_from_stdin(), ("Kitchen WiFi", "secret"))
        finally:
            __import__("sys").stdin = original_stdin

    def test_reads_combined_setup_from_stdin(self) -> None:
        original_stdin = __import__("sys").stdin
        document = {
            "api_base_url": "https://foodlog-api-sptvo5nsga-ew.a.run.app",
            "camera_id": "12345678-1234-1234-1234-123456789abc",
            "authorization": "FoodLogCamera flc_v1_camera-secret",
            "wifi_ssid": "Kitchen WiFi",
            "wifi_password": "secret",
        }
        try:
            __import__("sys").stdin = io.StringIO(json.dumps(document))
            setup, ssid, password = read_setup_and_wifi_from_stdin()
            self.assertEqual(setup.camera_id, document["camera_id"])
            self.assertEqual((ssid, password), ("Kitchen WiFi", "secret"))
        finally:
            __import__("sys").stdin = original_stdin


if __name__ == "__main__":
    unittest.main()
