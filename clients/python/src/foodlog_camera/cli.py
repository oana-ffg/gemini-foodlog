from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

from .client import FoodLogCameraClient, read_credential_file
from .images import Frame, fixture_frames, webcam_frames

PRODUCTION_API_BASE = "https://foodlog-api-sptvo5nsga-ew.a.run.app"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload FoodLog camera frames securely")
    parser.add_argument("--api-base", default=PRODUCTION_API_BASE)
    parser.add_argument("--camera-id", required=True)
    parser.add_argument("--credential-file", type=Path)
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("status", help="Verify that the camera credential is active")

    fixture = subcommands.add_parser("fixture", help="Replay one or more JPEG/PNG files")
    fixture.add_argument("images", nargs="+", type=Path)
    fixture.add_argument("--interval-seconds", type=non_negative_float, default=0)

    webcam = subcommands.add_parser("webcam", help="Capture a bounded webcam sequence")
    webcam.add_argument("--device-index", type=int, default=0)
    webcam.add_argument("--count", type=positive_int, default=1)
    webcam.add_argument("--interval-seconds", type=non_negative_float, default=1)
    webcam.add_argument("--width", type=positive_int, default=1920)
    webcam.add_argument("--height", type=positive_int, default=1080)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    credential = load_credential(args.credential_file)
    with FoodLogCameraClient(
        api_base=args.api_base,
        camera_id=args.camera_id,
        credential=credential,
    ) as client:
        if args.command == "status":
            print(json.dumps(client.status(), sort_keys=True))
            return 0
        sequence_id = f"python-{uuid4()}"
        frames = frames_for_command(args)
        for sequence_number, frame in enumerate(frames):
            accepted = client.upload_capture(
                image=frame.image,
                content_type=frame.content_type,
                width=frame.width,
                height=frame.height,
                captured_at=frame.captured_at,
                sequence_id=sequence_id,
                sequence_number=sequence_number,
            )
            print(json.dumps({
                "accepted_image_count": accepted.accepted_image_count,
                "capture_id": accepted.capture_id,
                "duplicate": accepted.duplicate,
                "sequence_number": sequence_number,
            }, sort_keys=True))
    return 0


def load_credential(credential_file: Path | None) -> str:
    if credential_file is not None:
        return read_credential_file(credential_file)
    credential = os.environ.get("FOODLOG_CAMERA_CREDENTIAL")
    if credential:
        return credential
    raise SystemExit(
        "Set FOODLOG_CAMERA_CREDENTIAL or provide --credential-file; never put the credential "
        "directly in the command line."
    )


def frames_for_command(args: argparse.Namespace) -> Iterable[Frame]:
    if args.command == "fixture":
        return fixture_frames(args.images, args.interval_seconds)
    if args.command == "webcam":
        return webcam_frames(
            device_index=args.device_index,
            count=args.count,
            interval_seconds=args.interval_seconds,
            width=args.width,
            height=args.height,
        )
    raise AssertionError(f"unsupported frame command: {args.command}")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
