"""Mix one approved Lyria experiment beneath the narration-first video master."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASE = (
    REPOSITORY_ROOT / "artifacts" / "demo-video" / "foodlog-demo-remotion-corrected.mp4"
)
SCORE_DIRECTORY = REPOSITORY_ROOT / "artifacts" / "demo-video" / "lyria" / "experiments"
OUTPUT_DIRECTORY = REPOSITORY_ROOT / "artifacts" / "demo-video" / "lyria-comparisons"
VARIANTS = (
    "uplifting-techno",
    "ai-product-ad",
    "human-tech-trailer",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", required=True, choices=VARIANTS)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional comparison path; defaults to the stable variant filename.",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def duration_seconds(path: Path) -> float:
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
    duration = float(result.stdout.strip())
    if duration <= 10:
        raise RuntimeError(f"Unexpected score duration for {path}: {duration}")
    return duration


def main() -> None:
    args = parse_args()
    base = args.base.resolve()
    score = SCORE_DIRECTORY / f"{args.variant}.mp3"
    output = (
        args.output.resolve()
        if args.output is not None
        else OUTPUT_DIRECTORY / f"foodlog-demo-{args.variant}.mp4"
    )
    for required in (base, score):
        if not required.is_file():
            raise FileNotFoundError(required)
    if output.exists() and not args.force:
        raise FileExistsError(f"Refusing to overwrite comparison: {output}")

    base_duration = duration_seconds(base)
    score_duration = duration_seconds(score)
    first_end = score_duration - 15.0
    if first_end <= 10.0:
        raise RuntimeError(f"Score is too short to extend cleanly: {score_duration}")
    fade_start = base_duration - 10.0
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.stem}.tmp.mp4")
    temporary.unlink(missing_ok=True)

    filter_graph = (
        "[0:a]asplit=2[narration][sidechain];"
        "[1:a]asplit=2[music_first][music_second];"
        f"[music_first]atrim=start=0:end={first_end:.6f},"
        "asetpts=PTS-STARTPTS[first];"
        "[music_second]atrim=start=8,asetpts=PTS-STARTPTS[second];"
        "[first][second]acrossfade=d=6:c1=tri:c2=tri[extended];"
        f"[extended]atrim=start=0:end={base_duration:.6f},"
        "asetpts=PTS-STARTPTS,"
        "aformat=sample_rates=48000:channel_layouts=stereo,"
        "highpass=f=80,lowpass=f=10500,"
        "equalizer=f=2200:t=q:w=0.8:g=-5,"
        "volume=0.04,"
        "afade=t=in:st=0:d=2,"
        f"afade=t=out:st={fade_start:.6f}:d=10[music];"
        "[music][sidechain]sidechaincompress="
        "threshold=0.015:ratio=10:attack=15:release=350:knee=4[ducked];"
        "[narration][ducked]amix="
        "inputs=2:duration=first:dropout_transition=0:normalize=0,"
        "loudnorm=I=-16:LRA=11:TP=-1.5,aresample=48000[audio]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(base),
            "-i",
            str(score),
            "-filter_complex",
            filter_graph,
            "-map",
            "0:v:0",
            "-map",
            "[audio]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
        check=True,
    )
    temporary.chmod(0o600)
    temporary.replace(output)
    print(f"Saved {output}")


if __name__ == "__main__":
    main()
