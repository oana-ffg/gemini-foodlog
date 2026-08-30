#!/usr/bin/env python3
"""Generate content-addressed Gemini narration files for the Remotion demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import wave
from datetime import UTC, datetime
from pathlib import Path

from google import genai
from google.genai import types


MODEL = "gemini-3.1-flash-tts-preview"
VOICE = "Sulafat"
PROJECT = "gemini-foodlog-2026"
LOCATION = "global"
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
VIDEO_ROOT = SCRIPT_DIRECTORY.parent
CONTENT_PATH = VIDEO_ROOT / "src" / "content.json"
MANIFEST_PATH = VIDEO_ROOT / "src" / "generated" / "tts-manifest.json"
AUDIO_DIRECTORY = VIDEO_ROOT / "public" / "generated" / "tts"

GLOBAL_DIRECTION = """# AUDIO PROFILE
A warm adult woman with emotional intelligence and excellent English diction.
She sounds like one thoughtful person sharing something she genuinely built and
cares about: intimate, conversational, and grounded, never like a glossy
commercial announcer.

# SCENE
English narration for a heartfelt product-demonstration film.

# DIRECTOR'S NOTES
Use natural pauses and varied intonation. Do not add, remove, or paraphrase
words. Do not add sound effects, music, laughter, or spoken headings.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", help="Generate only one scene ID")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def content_hash(*, narration: str, direction: str) -> str:
    canonical = json.dumps(
        {
            "model": MODEL,
            "voice": VOICE,
            "narration": narration,
            "direction": direction,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_wave(destination: Path, pcm: bytes, *, channels: int, sample_rate: int) -> None:
    temporary = destination.with_suffix(".wav.tmp")
    with wave.open(str(temporary), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    temporary.chmod(0o600)
    temporary.replace(destination)


def measured_duration(destination: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise RuntimeError(f"Could not measure narration duration for {destination.name}")
    return duration


def main() -> None:
    args = parse_args()
    content = json.loads(CONTENT_PATH.read_text())
    manifest = json.loads(MANIFEST_PATH.read_text())
    scenes = [scene for scene in content["scenes"] if not args.scene or scene["id"] == args.scene]
    if not scenes:
        raise ValueError(f"Unknown scene: {args.scene}")

    specs = []
    for scene in scenes:
        direction = f"{GLOBAL_DIRECTION}\nScene-specific direction: {scene['voiceDirection']}"
        digest = content_hash(narration=scene["narration"], direction=direction)
        specs.append(
            {
                "scene": scene,
                "direction": direction,
                "hash": digest,
                "file": f"generated/tts/{scene['id']}-{digest[:12]}.wav",
            }
        )

    pending = [
        spec
        for spec in specs
        if args.force
        or manifest.get("entries", {}).get(spec["scene"]["id"], {}).get("hash") != spec["hash"]
    ]
    if not pending:
        print("All selected narration files already match their text, voice, model, and direction.")
        return

    AUDIO_DIRECTORY.mkdir(parents=True, exist_ok=True)
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    entries = dict(manifest.get("entries", {}))

    for spec in pending:
        scene = spec["scene"]
        prompt = (
            f"{spec['direction']}\n\n# TRANSCRIPT\n"
            f"<transcript>{scene['narration']}</transcript>"
        )
        print(f"Generating narration: {scene['id']}", flush=True)
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
                    )
                ),
            ),
        )
        parts = response.candidates[0].content.parts if response.candidates else []
        audio = next((part.inline_data for part in parts if part.inline_data), None)
        if not audio or not audio.data:
            raise RuntimeError(f"Gemini returned no audio for {scene['id']}")
        destination = VIDEO_ROOT / "public" / spec["file"]
        pcm = audio.data
        write_wave(
            destination,
            pcm,
            channels=1,
            sample_rate=24_000,
        )
        duration = measured_duration(destination)
        usage = response.usage_metadata
        entries[scene["id"]] = {
            "hash": spec["hash"],
            "file": spec["file"],
            "durationSeconds": duration,
            "characters": len(scene["narration"]),
            "inputTokens": usage.prompt_token_count if usage else None,
            "outputTokens": usage.candidates_token_count if usage else None,
        }

    MANIFEST_PATH.write_text(
        json.dumps(
            {
                "generatedAt": datetime.now(UTC).isoformat(),
                "model": MODEL,
                "voice": VOICE,
                "entries": entries,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Updated {MANIFEST_PATH}; Vertex AI used ambient Google credentials.")


if __name__ == "__main__":
    main()
