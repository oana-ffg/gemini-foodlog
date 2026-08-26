from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass

from google import genai
from google.genai import types

EXPECTED_RESPONSE = "FOODLOG_AI001_OK"
DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_LOCATION = "eu"
NON_GLOBAL_INPUT_USD_PER_MILLION = 0.825
NON_GLOBAL_OUTPUT_USD_PER_MILLION = 4.125


@dataclass(frozen=True)
class ProbeResult:
    model: str
    location: str
    response: str
    prompt_tokens: int
    response_tokens: int
    thinking_tokens: int
    total_tokens: int
    estimated_cost_usd: float


def required_project(value: str | None) -> str:
    if value is None or not value.strip():
        raise ValueError("--project or GOOGLE_CLOUD_PROJECT is required")
    return value.strip()


def token_count(value: int | None) -> int:
    return value if value is not None else 0


def estimate_cost_usd(*, prompt_tokens: int, response_tokens: int, thinking_tokens: int) -> float:
    return (
        prompt_tokens * NON_GLOBAL_INPUT_USD_PER_MILLION
        + (response_tokens + thinking_tokens) * NON_GLOBAL_OUTPUT_USD_PER_MILLION
    ) / 1_000_000


def run_probe(*, project: str, location: str, model: str) -> ProbeResult:
    client = genai.Client(
        vertexai=True,
        project=project,
        location=location,
        http_options=types.HttpOptions(api_version="v1"),
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=(
                "This is a bounded deployment verification. Reply with exactly "
                f"{EXPECTED_RESPONSE} and nothing else."
            ),
            config=types.GenerateContentConfig(
                max_output_tokens=32,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                thinking_config=types.ThinkingConfig(
                    thinking_level=types.ThinkingLevel.MINIMAL,
                ),
            ),
        )
    finally:
        client.close()

    response_text = (response.text or "").strip()
    if response_text != EXPECTED_RESPONSE:
        raise RuntimeError(f"unexpected model response: {response_text!r}")

    usage = response.usage_metadata
    if usage is None:
        raise RuntimeError("successful model response omitted usage metadata")
    prompt_tokens = token_count(usage.prompt_token_count)
    response_tokens = token_count(usage.candidates_token_count)
    thinking_tokens = token_count(usage.thoughts_token_count)
    total_tokens = token_count(usage.total_token_count)
    if prompt_tokens == 0 or response_tokens == 0 or total_tokens == 0:
        raise RuntimeError("successful model response reported incomplete token usage")

    estimated_cost_usd = estimate_cost_usd(
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        thinking_tokens=thinking_tokens,
    )
    return ProbeResult(
        model=model,
        location=location,
        response=response_text,
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        thinking_tokens=thinking_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one bounded, billable Gemini availability probe through Vertex AI",
    )
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument(
        "--location",
        default=os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION),
    )
    parser.add_argument("--model", default=os.environ.get("FOODLOG_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--confirm-billable-probe",
        action="store_true",
        help="required safety acknowledgement before the request is sent",
    )
    args = parser.parse_args()
    if not args.confirm_billable_probe:
        parser.error("--confirm-billable-probe is required")

    result = run_probe(
        project=required_project(args.project),
        location=args.location,
        model=args.model,
    )
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
