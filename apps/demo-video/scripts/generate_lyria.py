"""Generate the one explicitly approved Lyria score without retries."""

from __future__ import annotations

import base64
from pathlib import Path

from google import genai
from google.genai import types

PROJECT = "gemini-foodlog-2026"
LOCATION = "global"
MODEL = "lyria-3-pro-preview"
OUTPUT = Path(__file__).resolve().parents[3] / "artifacts" / "demo-video" / "lyria" / "foodlog-score.mp3"
PROMPT = """Create a 184-second instrumental underscore for a heartfelt technology documentary. No vocals, lyrics, spoken words, choir, humming, sound effects, or recognizable existing melody. The music must sit quietly beneath a warm woman narrator, with a spacious midrange and no attention-grabbing lead.

[0:00-0:18] Intimate and empathetic: sparse felt piano, soft warm analogue pad, gentle room texture, around 76 BPM. Acknowledge that tracking food while unwell and overwhelmed is difficult, without becoming sad or medical.

[0:18-0:35] A small hopeful lift as a solution appears: add a delicate muted pulse and subtle marimba-like organic plucks, still restrained.

[0:35-1:22] Quiet forward motion for an engineering explanation: warm minimal electronic pulse, soft pizzicato details, clean and intelligent, no corporate triumph or trailer drama.

[1:22-2:18] More human and reassuring for uncertainty, corrections, and learning: return the felt piano motif with gentle strings and organic texture. Keep narration completely clear.

[2:18-2:48] Curious, lightly playful warmth for a cat negative control and patterns over time, never whimsical comedy.

[2:48-3:04] Restrained emotional resolution: broaden the warm harmony, briefly reprise the piano motif, then decrescendo to a clean, complete ending by 3:04. Quiet confidence, not a commercial crescendo.

Cohesive single composition, natural transitions, modern but timeless, emotionally sincere, low dynamic range suitable for dialogue mixing."""


def decoded_audio(data: str | bytes) -> bytes:
    if isinstance(data, str):
        return base64.b64decode(data)
    if data.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return data
    return base64.b64decode(data)


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"Refusing to overwrite existing approved generation: {OUTPUT}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

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
    interaction = client.interactions.create(model=MODEL, input=PROMPT, timeout=240)
    if not interaction.output_audio or not interaction.output_audio.data:
        raise RuntimeError("Lyria returned no audio")
    audio = decoded_audio(interaction.output_audio.data)
    if len(audio) < 1_000 or not audio.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        raise RuntimeError("Lyria returned an invalid MP3 payload")
    temporary = OUTPUT.with_suffix(".mp3.tmp")
    temporary.write_bytes(audio)
    temporary.chmod(0o600)
    temporary.replace(OUTPUT)
    print(f"Saved {OUTPUT}")


if __name__ == "__main__":
    main()
