import sys
from pathlib import Path
from types import SimpleNamespace

from foodlog_camera.images import load_fixture, webcam_frames

FIXTURE = (
    Path(__file__).parents[3]
    / "tests/fixtures/images/adversarial/synthetic-distant-ambiguous-meat-pack.png"
)


def test_load_fixture_reads_real_dimensions_and_mime() -> None:
    frame = load_fixture(FIXTURE)
    assert frame.content_type == "image/png"
    assert (frame.width, frame.height) == (1659, 948)
    assert frame.image.startswith(b"\x89PNG\r\n\x1a\n")


def test_webcam_sequence_releases_device(monkeypatch) -> None:
    class FakeCapture:
        released = False

        def isOpened(self) -> bool:
            return True

        def set(self, *_: object) -> bool:
            return True

        def read(self):
            return True, SimpleNamespace(shape=(480, 640, 3))

        def release(self) -> None:
            self.released = True

    class Encoded:
        def tobytes(self) -> bytes:
            return b"jpeg-bytes"

    capture = FakeCapture()
    fake_cv2 = SimpleNamespace(
        CAP_PROP_FRAME_WIDTH=1,
        CAP_PROP_FRAME_HEIGHT=2,
        IMWRITE_JPEG_QUALITY=3,
        VideoCapture=lambda _: capture,
        imencode=lambda *_: (True, Encoded()),
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    frames = list(webcam_frames(
        device_index=0,
        count=2,
        interval_seconds=0,
        width=1920,
        height=1080,
    ))

    assert [(frame.width, frame.height) for frame in frames] == [(640, 480), (640, 480)]
    assert all(frame.image == b"jpeg-bytes" for frame in frames)
    assert capture.released is True
