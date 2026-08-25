from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image


@dataclass(frozen=True, slots=True)
class Frame:
    image: bytes
    content_type: str
    width: int
    height: int
    captured_at: datetime


def load_fixture(path: Path) -> Frame:
    image_bytes = path.read_bytes()
    with Image.open(path) as image:
        width, height = image.size
        content_type = Image.MIME.get(image.format or "")
        image.verify()
    if content_type not in {"image/jpeg", "image/png"}:
        raise ValueError(f"{path} is not a supported JPEG or PNG image")
    return Frame(
        image=image_bytes,
        content_type=content_type,
        width=width,
        height=height,
        captured_at=datetime.now(UTC),
    )


def fixture_frames(paths: list[Path], interval_seconds: float) -> Iterator[Frame]:
    for index, path in enumerate(paths):
        if index:
            time.sleep(interval_seconds)
        yield load_fixture(path)


def webcam_frames(
    *,
    device_index: int,
    count: int,
    interval_seconds: float,
    width: int,
    height: int,
) -> Iterator[Frame]:
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError(
            "Webcam mode requires the optional webcam dependency: uv sync --extra webcam"
        ) from error

    camera = cv2.VideoCapture(device_index)
    if not camera.isOpened():
        camera.release()
        raise RuntimeError(f"Could not open webcam device {device_index}")
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    try:
        for index in range(count):
            if index:
                time.sleep(interval_seconds)
            captured, frame = camera.read()
            if not captured:
                raise RuntimeError("The webcam did not return a frame")
            encoded, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if not encoded:
                raise RuntimeError("The webcam frame could not be encoded")
            frame_height, frame_width = frame.shape[:2]
            yield Frame(
                image=jpeg.tobytes(),
                content_type="image/jpeg",
                width=int(frame_width),
                height=int(frame_height),
                captured_at=datetime.now(UTC),
            )
    finally:
        camera.release()
