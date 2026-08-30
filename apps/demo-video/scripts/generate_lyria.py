"""Generate one explicitly approved named Lyria score without retries."""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

from google import genai
from google.genai import types

PROJECT = "gemini-foodlog-2026"
LOCATION = "global"
MODEL = "lyria-3-pro-preview"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROMPT_DIRECTORY = Path(__file__).resolve().parent / "lyria-prompts"
OUTPUT_DIRECTORY = REPOSITORY_ROOT / "artifacts" / "demo-video" / "lyria" / "experiments"
VARIANTS = (
    "uplifting-techno",
    "ai-product-ad",
    "human-tech-trailer",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", required=True, choices=VARIANTS)
    return parser.parse_args()


def decoded_audio(data: str | bytes) -> bytes:
    if isinstance(data, str):
        return base64.b64decode(data)
    if data.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return data
    return base64.b64decode(data)


def main() -> None:
    args = parse_args()
    prompt_path = PROMPT_DIRECTORY / f"{args.variant}.txt"
    prompt = prompt_path.read_text().strip()
    output = OUTPUT_DIRECTORY / f"{args.variant}.mp3"
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing approved generation: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    http_options = types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=1),
        timeout=240_000,
    )
    client = genai.Client(
        vertexai=True,
        project=PROJECT,
        location=LOCATION,
        http_options=http_options,
    )
    interaction = client.interactions.create(model=MODEL, input=prompt, timeout=240)
    if not interaction.output_audio or not interaction.output_audio.data:
        raise RuntimeError("Lyria returned no audio")
    audio = decoded_audio(interaction.output_audio.data)
    if len(audio) < 1_000 or not audio.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        raise RuntimeError("Lyria returned an invalid MP3 payload")
    temporary = output.with_suffix(".mp3.tmp")
    temporary.write_bytes(audio)
    temporary.chmod(0o600)
    temporary.replace(output)
    print(f"Saved {output}")


if __name__ == "__main__":
    main()
