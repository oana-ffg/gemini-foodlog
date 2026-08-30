#!/usr/bin/env python3
"""Generate one explicitly approved final-video Veo shot, exactly once."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from google import genai
from google.genai import types


PROJECT = "gemini-foodlog-2026"
LOCATION = "us-central1"
MODEL = "veo-3.1-lite-generate-001"
OUTPUT_DIRECTORY = Path(__file__).resolve().parents[3] / "artifacts" / "demo-video" / "veo"

SCENES = {
    "intro-cooking": """Warm, emotionally grounded documentary footage in an ordinary lived-in home kitchen at dinner time. Wide side view from a fixed inexpensive camera around two metres away. An adult woman cooks naturally at the counter and stove, moving between vegetables, a pan, and a simple meal without posing for the camera or using a phone. Only natural continuous activity. Soft late-afternoon window light mixed with practical kitchen light, realistic textures, subtly cinematic but not glossy advertising. No readable labels, no logos, no text, no medical imagery, no identifiable private information, no dialogue, no audio. One continuous eight-second 16:9 shot.""",
    "intro-chaos": """Emotionally truthful but gently comedic documentary footage in a messy lived-in kitchen after dinner. Medium-wide side view. An adult woman sits at the table holding her aching temple with her left hand while trying to type a food log into her smartphone with her right hand. She looks exhausted and increasingly exasperated. In the final second she abruptly slams the phone face-down onto the wooden table in frustration. The phone remains intact: no broken glass, injury, self-harm, or dangerous debris. Natural practical light, realistic home texture, one continuous action, no melodramatic acting, no readable screen, no logos, no text, no dialogue, no audio. One continuous eight-second 16:9 shot.""",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scene", choices=sorted(SCENES))
    parser.add_argument(
        "--operation-name",
        help="Resume an already-submitted Vertex operation without generating again",
    )
    args = parser.parse_args()

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT_DIRECTORY / f"{args.scene}.mp4"
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing approved generation: {destination}")

    retry_once = types.HttpRetryOptions(attempts=1)
    http_options = types.HttpOptions(retry_options=retry_once, timeout=120_000)
    client = genai.Client(
        vertexai=True,
        project=PROJECT,
        location=LOCATION,
        http_options=http_options,
    )
    if args.operation_name:
        operation = client.operations.get(types.GenerateVideosOperation(name=args.operation_name))
        print(f"Resuming {args.scene}: {operation.name}", flush=True)
    else:
        operation = client.models.generate_videos(
            model=MODEL,
            source=types.GenerateVideosSource(prompt=SCENES[args.scene]),
            config=types.GenerateVideosConfig(
                number_of_videos=1,
                duration_seconds=8,
                aspect_ratio="16:9",
                resolution="720p",
                person_generation="allow_adult",
                generate_audio=False,
                http_options=http_options,
            ),
        )
        print(f"Submitted {args.scene}: {operation.name}", flush=True)
        destination.with_suffix(".operation").write_text(f"{operation.name}\n")
    while not operation.done:
        time.sleep(15)
        operation = client.operations.get(operation)
        print(f"Waiting for {args.scene}…", flush=True)

    if operation.error:
        raise RuntimeError(f"Veo rejected {args.scene}: {operation.error}")
    generated = operation.result.generated_videos if operation.result else []
    if len(generated) != 1 or not generated[0].video or not generated[0].video.video_bytes:
        raise RuntimeError(f"Veo returned {len(generated)} videos for {args.scene}; expected one")
    destination.write_bytes(generated[0].video.video_bytes)
    destination.chmod(0o600)
    print(f"Saved {destination}")


if __name__ == "__main__":
    main()
