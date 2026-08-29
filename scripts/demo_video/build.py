from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

WIDTH = 1920
HEIGHT = 1080
FPS = 30
BACKGROUND = "#f3efe6"
INK = "#17201b"
GREEN = "#163f31"
ORANGE = "#f05a35"
FONT_REGULAR = Path("/System/Library/Fonts/SFNS.ttf")
FONT_ROUNDED = Path("/System/Library/Fonts/SFNSRounded.ttf")

SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parents[1]
SEGMENTS_PATH = SCRIPT_DIR / "segments.json"
ARCHITECTURE_PATH = REPOSITORY_ROOT / "docs" / "architecture-diagram.md"
OPENING_FIXTURE_PATH = (
    REPOSITORY_ROOT
    / "tests"
    / "fixtures"
    / "images"
    / "adversarial"
    / "synthetic-distant-ambiguous-meat-pack-v2.png"
)


@dataclass(frozen=True)
class Segment:
    key: str
    kind: str
    minimum_seconds: float
    heading: str
    narration: str
    source: str | None = None
    secondary_source: str | None = None


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _require_tools() -> None:
    missing = [
        tool for tool in ("dot", "ffmpeg", "ffprobe", "say") if not shutil.which(tool)
    ]
    if missing:
        raise RuntimeError(f"missing required demo tools: {', '.join(missing)}")
    for font in (FONT_REGULAR, FONT_ROUNDED):
        if not font.is_file():
            raise RuntimeError(f"required macOS font is missing: {font}")


def _load_segments() -> list[Segment]:
    raw = json.loads(SEGMENTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("segments.json must contain a non-empty list")
    segments: list[Segment] = []
    keys: set[str] = set()
    for item in raw:
        segment = Segment(**item)
        if segment.key in keys:
            raise RuntimeError(f"duplicate segment key: {segment.key}")
        if segment.minimum_seconds <= 0:
            raise RuntimeError(f"segment {segment.key} has no positive time budget")
        if not segment.narration.strip():
            raise RuntimeError(f"segment {segment.key} has empty narration")
        keys.add(segment.key)
        segments.append(segment)
    return segments


def _validate_shots(shots_dir: Path, segments: list[Segment]) -> None:
    sources = {
        name
        for segment in segments
        for name in (segment.source, segment.secondary_source)
        if name
    }
    for name in sorted(sources):
        path = shots_dir / name
        if not path.is_file():
            raise RuntimeError(f"required reviewed production shot is missing: {path}")
        with Image.open(path) as image:
            if image.width < WIDTH or image.height < HEIGHT:
                raise RuntimeError(
                    f"shot {path} is {image.width}x{image.height}; "
                    f"at least {WIDTH}x{HEIGHT} is required"
                )


def _font(size: int, *, rounded: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_ROUNDED if rounded else FONT_REGULAR), size)


def _fit_full_image(path: Path, *, background: str = BACKGROUND) -> Image.Image:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    canvas = Image.new("RGB", (WIDTH, HEIGHT), background)
    fitted = ImageOps.contain(image, (WIDTH, HEIGHT - 138), Image.Resampling.LANCZOS)
    canvas.paste(fitted, ((WIDTH - fitted.width) // 2, 138 + (HEIGHT - 138 - fitted.height) // 2))
    return canvas


def _draw_heading(canvas: Image.Image, heading: str) -> None:
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, WIDTH, 138), fill=GREEN)
    draw.text((70, 26), "GEMINI FOODLOG", font=_font(24, rounded=True), fill=ORANGE)
    draw.text((70, 62), heading, font=_font(48, rounded=True), fill="#fffdf7")


def _render_shot(source: Path, heading: str, output: Path) -> None:
    canvas = _fit_full_image(source)
    _draw_heading(canvas, heading)
    canvas.save(output, format="PNG", optimize=True)


def _render_pair(primary: Path, secondary: Path, heading: str, output: Path) -> None:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    for index, source_path in enumerate((primary, secondary)):
        with Image.open(source_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        fitted = ImageOps.contain(image, (910, 880), Image.Resampling.LANCZOS)
        x = 40 + index * 965 + (910 - fitted.width) // 2
        y = 158 + (880 - fitted.height) // 2
        canvas.paste(fitted, (x, y))
    draw = ImageDraw.Draw(canvas)
    draw.line((960, 172, 960, 1020), fill="#d5cec0", width=3)
    _draw_heading(canvas, heading)
    canvas.save(output, format="PNG", optimize=True)


def _render_opening(heading: str, output: Path) -> None:
    with Image.open(OPENING_FIXTURE_PATH) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    background = ImageOps.fit(image, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    background = ImageEnhance.Brightness(background).enhance(0.46)
    background = background.filter(ImageFilter.GaussianBlur(radius=1.5))
    draw = ImageDraw.Draw(background, "RGBA")
    draw.rectangle((0, 0, WIDTH, HEIGHT), fill=(7, 26, 20, 105))
    draw.text((100, 100), "GEMINI FOODLOG", font=_font(34, rounded=True), fill=ORANGE)
    wrapped = textwrap.wrap(heading, width=27)
    y = 260
    for line in wrapped:
        draw.text((100, y), line, font=_font(92, rounded=True), fill="#fffdf7")
        y += 108
    draw.text(
        (105, y + 35),
        "Ordinary kitchen activity → an evidence-linked food timeline",
        font=_font(32),
        fill="#e9e4d9",
    )
    background.save(output, format="PNG", optimize=True)


def _extract_mermaid() -> str:
    source = ARCHITECTURE_PATH.read_text(encoding="utf-8")
    match = re.search(r"```mermaid\n(.*?)\n```", source, flags=re.DOTALL)
    if not match:
        raise RuntimeError("canonical architecture document contains no Mermaid block")
    return match.group(1)


def _mermaid_to_dot(mermaid: str) -> str:
    nodes: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    group_labels: dict[str, str] = {}
    edges: list[tuple[str, str, str, bool]] = []
    current_group: str | None = None

    for raw_line in mermaid.splitlines():
        line = raw_line.strip()
        subgraph = re.fullmatch(r'subgraph\s+(\w+)\["(.*)"\]', line)
        if subgraph:
            current_group = subgraph.group(1)
            groups[current_group] = []
            group_labels[current_group] = subgraph.group(2)
            continue
        if line == "end":
            current_group = None
            continue

        node = re.match(r'^(\w+)\s*\[.*?"(.*?)".*\]\s*$', line)
        if node and "-->" not in line and "-." not in line:
            node_id, label = node.groups()
            nodes[node_id] = label.replace("<br/>", "\\n")
            if current_group:
                groups[current_group].append(node_id)
            continue

        if "-->" in line:
            left, right = line.split("-->", 1)
            label = ""
            label_match = re.match(r"\|(.*?)\|\s*(\w+)$", right.strip())
            if label_match:
                label, destination = label_match.groups()
            else:
                destination = right.strip()
            for source_id in (part.strip() for part in left.split("&")):
                edges.append((source_id, destination, label, False))
            continue

        dashed = re.match(r"^(.*?)\s+-\.\s*(.*?)\s*\.->\s*(\w+)$", line)
        if dashed:
            left, label, destination = dashed.groups()
            for source_id in (part.strip() for part in left.split("&")):
                edges.append((source_id, destination, label, True))

    if len(nodes) < 20 or len(edges) < 25:
        raise RuntimeError(
            f"architecture parser found only {len(nodes)} nodes and {len(edges)} edges"
        )

    def quoted(value: str) -> str:
        return json.dumps(value)

    lines = [
        "digraph FoodLog {",
        'graph [bgcolor="#f3efe6", rankdir=TB, pad=0.25, nodesep=0.26, ranksep=0.38];',
        'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, color="#315548", fillcolor="#fffdf7", fontcolor="#17201b", margin="0.12,0.08"];',
        'edge [fontname="Helvetica", fontsize=8, color="#557066", fontcolor="#315548", arrowsize=0.65];',
    ]
    grouped_nodes = {node_id for values in groups.values() for node_id in values}
    for group_id, node_ids in groups.items():
        lines.extend(
            [
                f"subgraph cluster_{group_id} {{",
                f"label={quoted(group_labels[group_id])};",
                'color="#b8c6bf"; style="rounded,filled"; fillcolor="#e9eee9";',
            ]
        )
        lines.extend(
            f"{node_id} [label={quoted(nodes[node_id])}];" for node_id in node_ids
        )
        lines.append("}")
    for node_id, label in nodes.items():
        if node_id not in grouped_nodes:
            lines.append(f"{node_id} [label={quoted(label)}];")
    for source, destination, label, dashed_edge in edges:
        attributes = []
        if label:
            attributes.append(f"label={quoted(label)}")
        if dashed_edge:
            attributes.append('style="dashed"')
        suffix = f" [{', '.join(attributes)}]" if attributes else ""
        lines.append(f"{source} -> {destination}{suffix};")
    lines.append("}")
    return "\n".join(lines)


def _render_architecture(heading: str, output: Path, work_dir: Path) -> None:
    dot_path = work_dir / "architecture.dot"
    graph_path = work_dir / "architecture.png"
    dot_path.write_text(_mermaid_to_dot(_extract_mermaid()), encoding="utf-8")
    _run(["dot", "-Tpng", "-Gdpi=150", str(dot_path), "-o", str(graph_path)])
    _render_shot(graph_path, heading, output)


def _render_closing(source: Path, heading: str, output: Path) -> None:
    with Image.open(source) as raw_source:
        source_image = ImageOps.exif_transpose(raw_source).convert("RGB")
    background = ImageOps.fit(source_image, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    background = ImageEnhance.Brightness(background).enhance(0.36)
    background = background.filter(ImageFilter.GaussianBlur(radius=2))
    draw = ImageDraw.Draw(background, "RGBA")
    draw.rectangle((0, 0, WIDTH, HEIGHT), fill=(7, 26, 20, 120))
    lines = textwrap.wrap(heading, width=28)
    y = 300
    for line in lines:
        bounds = draw.textbbox((0, 0), line, font=_font(84, rounded=True))
        x = (WIDTH - (bounds[2] - bounds[0])) // 2
        draw.text((x, y), line, font=_font(84, rounded=True), fill="#fffdf7")
        y += 102
    subtitle = "Clear provenance. Durable feedback. Household-scoped learning."
    bounds = draw.textbbox((0, 0), subtitle, font=_font(30))
    draw.text(((WIDTH - (bounds[2] - bounds[0])) // 2, y + 28), subtitle, font=_font(30), fill="#e9e4d9")
    background.save(output, format="PNG", optimize=True)


def _render_segment_image(
    segment: Segment, shots_dir: Path, output: Path, work_dir: Path
) -> None:
    if segment.kind == "opening":
        _render_opening(segment.heading, output)
    elif segment.kind == "architecture":
        _render_architecture(segment.heading, output, work_dir)
    elif segment.kind == "shot":
        assert segment.source
        _render_shot(shots_dir / segment.source, segment.heading, output)
    elif segment.kind == "shot_pair":
        assert segment.source and segment.secondary_source
        _render_pair(
            shots_dir / segment.source,
            shots_dir / segment.secondary_source,
            segment.heading,
            output,
        )
    elif segment.kind == "closing":
        assert segment.source
        _render_closing(shots_dir / segment.source, segment.heading, output)
    else:
        raise RuntimeError(f"unknown segment kind: {segment.kind}")


def _audio_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _generate_narration(segment: Segment, output: Path, *, voice: str, rate: int) -> None:
    _run(
        [
            "say",
            "-v",
            voice,
            "-r",
            str(rate),
            "-o",
            str(output),
            segment.narration,
        ]
    )


def _render_video_segment(
    image_path: Path,
    audio_path: Path,
    output: Path,
    *,
    minimum_seconds: float,
) -> float:
    duration = max(minimum_seconds, _audio_duration(audio_path) + 1.0)
    fade_out = max(0.0, duration - 0.35)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(FPS),
            "-i",
            str(image_path),
            "-i",
            str(audio_path),
            "-vf",
            f"fade=t=in:st=0:d=0.35,fade=t=out:st={fade_out:.3f}:d=0.35,format=yuv420p",
            "-af",
            f"apad=whole_dur={duration:.3f}",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(FPS),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ]
    )
    return duration


def _concat_segments(segment_paths: list[Path], output: Path, work_dir: Path) -> None:
    concat_path = work_dir / "concat.txt"
    def concat_path_literal(path: Path) -> str:
        return "'" + str(path).replace("'", "'\\''") + "'"

    concat_path.write_text(
        "".join(f"file {concat_path_literal(path)}\n" for path in segment_paths),
        encoding="utf-8",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ]
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(*, shots_dir: Path, output: Path, voice: str, rate: int) -> dict[str, Any]:
    _require_tools()
    segments = _load_segments()
    _validate_shots(shots_dir, segments)
    with tempfile.TemporaryDirectory(prefix="foodlog-demo-") as temporary:
        work_dir = Path(temporary)
        videos: list[Path] = []
        measured_segments: list[dict[str, Any]] = []
        for index, segment in enumerate(segments, start=1):
            image_path = work_dir / f"{index:02d}-{segment.key}.png"
            audio_path = work_dir / f"{index:02d}-{segment.key}.aiff"
            video_path = work_dir / f"{index:02d}-{segment.key}.mp4"
            _render_segment_image(segment, shots_dir, image_path, work_dir)
            _generate_narration(segment, audio_path, voice=voice, rate=rate)
            duration = _render_video_segment(
                image_path,
                audio_path,
                video_path,
                minimum_seconds=segment.minimum_seconds,
            )
            videos.append(video_path)
            measured_segments.append({"key": segment.key, "seconds": round(duration, 3)})
        _concat_segments(videos, output, work_dir)

    duration = _audio_duration(output)
    if duration > 240:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"assembled demo is {duration:.3f}s and exceeds the four-minute limit"
        )
    return {
        "bytes": output.stat().st_size,
        "duration_seconds": round(duration, 3),
        "output": str(output.resolve()),
        "segments": measured_segments,
        "sha256": _sha256(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the private FoodLog hackathon demo from reviewed production shots."
    )
    parser.add_argument("--shots-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--voice", default="Samantha")
    parser.add_argument("--rate", type=int, default=165)
    args = parser.parse_args()
    report = build(
        shots_dir=args.shots_dir.resolve(),
        output=args.output.resolve(),
        voice=args.voice,
        rate=args.rate,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
