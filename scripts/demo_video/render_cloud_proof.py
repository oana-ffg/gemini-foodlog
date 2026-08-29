from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH = 2560
HEIGHT = 1440
FONT_REGULAR = Path("/System/Library/Fonts/SFNS.ttf")
FONT_MONO = Path("/System/Library/Fonts/SFNSMono.ttf")
FONT_ROUNDED = Path("/System/Library/Fonts/SFNSRounded.ttf")
REQUIRED_FIELDS = {
    "captured_at",
    "service",
    "revision",
    "traffic_percent",
    "image_digest",
    "model",
    "vertex_ai",
    "model_budget_dkk",
    "resources",
    "release_commit",
}


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    if not path.is_file():
        raise RuntimeError(f"required macOS font is missing: {path}")
    return ImageFont.truetype(str(path), size)


def _load_evidence(path: Path) -> dict[str, Any]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise TypeError("evidence source must be a JSON object")
    missing = sorted(REQUIRED_FIELDS - evidence.keys())
    if missing:
        raise RuntimeError(f"evidence source is missing: {', '.join(missing)}")
    if evidence["vertex_ai"] is not True:
        raise RuntimeError("evidence does not prove Vertex AI is enabled")
    if evidence["traffic_percent"] != 100:
        raise RuntimeError("evidence does not prove 100-percent revision traffic")
    digest = str(evidence["image_digest"])
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise RuntimeError("evidence image digest is not an immutable SHA-256 digest")
    return evidence


def render(evidence_path: Path, output: Path) -> dict[str, Any]:
    evidence = _load_evidence(evidence_path)
    canvas = Image.new("RGB", (WIDTH, HEIGHT), "#071a14")
    draw = ImageDraw.Draw(canvas)
    rounded = _font(FONT_ROUNDED, 86)
    label_font = _font(FONT_REGULAR, 30)
    mono = _font(FONT_MONO, 35)
    mono_small = _font(FONT_MONO, 29)

    draw.text((130, 95), "LIVE PRODUCTION PROOF", font=label_font, fill="#f05a35")
    draw.text((130, 145), "The deployed agent path", font=rounded, fill="#fffdf7")
    draw.text(
        (132, 255),
        f"Sanitized read-only capture · {evidence['captured_at']}",
        font=label_font,
        fill="#aabdb4",
    )

    rows = [
        ("Cloud Run service", evidence["service"]),
        ("Ready revision", evidence["revision"]),
        ("Traffic", f"{evidence['traffic_percent']}%"),
        ("Container", evidence["image_digest"]),
        ("Google model", evidence["model"]),
        ("Vertex AI", "enabled"),
        ("Resources", evidence["resources"]),
        ("Model hard cap", f"DKK {evidence['model_budget_dkk']}"),
        ("Release commit", evidence["release_commit"]),
    ]
    top = 365
    row_height = 94
    for index, (label, value) in enumerate(rows):
        y = top + index * row_height
        if index % 2 == 0:
            draw.rounded_rectangle(
                (105, y - 15, WIDTH - 105, y + 65),
                radius=18,
                fill="#102a21",
            )
        draw.text((145, y), label, font=label_font, fill="#85a395")
        value_font = mono_small if len(str(value)) > 52 else mono
        wrapped = textwrap.wrap(str(value), width=78)
        draw.text((725, y - (12 if len(wrapped) > 1 else 0)), wrapped[0], font=value_font, fill="#fffdf7")
        if len(wrapped) > 1:
            draw.text((725, y + 25), wrapped[1], font=value_font, fill="#fffdf7")

    draw.text(
        (130, HEIGHT - 120),
        "Cloud Run · Pub/Sub · Firestore · private Cloud Storage · Google ADK · Vertex AI",
        font=label_font,
        fill="#f4bd57",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="PNG", optimize=True)
    return {
        "bytes": output.stat().st_size,
        "output": str(output.resolve()),
        "source": str(evidence_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a sanitized, source-backed Cloud proof card for the demo."
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            render(args.evidence.resolve(), args.output.resolve()),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
