from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict

from foodlog_backend.model_probe import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL,
    required_project,
    run_probe,
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
