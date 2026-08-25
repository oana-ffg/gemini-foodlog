import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from foodlog_backend.models import CaptureEnvelopeV1

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def valid_envelope() -> dict[str, object]:
    return {
        "schema_version": 1,
        "camera_id": "camera-9bcd2f3a",
        "captured_at": "2026-08-25T16:04:05.123+02:00",
        "client_kind": "physical",
        "client_version": "esp32-foodlog/0.1.0",
        "sequence_id": "boot-20260825-0001",
        "sequence_number": 42,
        "burst_id": "motion-20260825-0007",
        "burst_frame_index": 3,
        "width": 1600,
        "height": 1200,
        "motion": {
            "detected": True,
            "algorithm": "frame-difference-v1",
            "score": 0.71,
            "changed_pixel_ratio": 0.18,
            "threshold": 0.12,
        },
    }


def test_capture_envelope_accepts_the_shared_device_shape() -> None:
    envelope = CaptureEnvelopeV1.model_validate(valid_envelope())

    assert envelope.schema_version == 1
    assert envelope.sequence_number == 42
    assert envelope.motion is not None
    assert envelope.motion.detected is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("captured_at", "2026-08-25T16:04:05"),
        ("sequence_id", "short"),
        ("sequence_number", -1),
        ("width", 0),
        ("height", 4097),
    ],
)
def test_capture_envelope_rejects_invalid_bounded_fields(
    field: str,
    value: object,
) -> None:
    payload = valid_envelope()
    payload[field] = value

    with pytest.raises(ValidationError):
        CaptureEnvelopeV1.model_validate(payload)


def test_capture_envelope_requires_burst_fields_together_and_rejects_extras() -> None:
    missing_index = valid_envelope()
    missing_index.pop("burst_frame_index")
    with pytest.raises(ValidationError, match="provided together"):
        CaptureEnvelopeV1.model_validate(missing_index)

    extra = valid_envelope()
    extra["account_id"] = "client-must-not-select-an-account"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CaptureEnvelopeV1.model_validate(extra)


def test_checked_in_contracts_match_the_application_models() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/generate_contracts.py", "--check"],
        cwd=BACKEND_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
