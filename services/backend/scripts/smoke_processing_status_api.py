from __future__ import annotations

import argparse
import os
from collections import Counter

import httpx

PROCESSING_STAGES = {
    "storage_pending",
    "grouping_pending",
    "grouping_active",
    "grouping_retrying",
    "analysis_pending",
    "analysis_active",
    "analysis_retrying",
    "complete",
    "attention_required",
}


def smoke(args: argparse.Namespace) -> None:
    firebase_response = httpx.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
        params={"key": args.firebase_api_key},
        headers={"Origin": args.origin, "Referer": f"{args.origin}/"},
        json={
            "email": os.environ["FOODLOG_SMOKE_EMAIL"],
            "password": os.environ["FOODLOG_SMOKE_PASSWORD"],
            "returnSecureToken": True,
        },
        timeout=30,
    )
    firebase_response.raise_for_status()
    token = firebase_response.json()["idToken"]

    unauthenticated = httpx.get(
        f"{args.api_url}/v1/processing?limit={args.limit}",
        timeout=30,
    )
    assert unauthenticated.status_code == 401, unauthenticated.text

    with httpx.Client(
        base_url=args.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        timeout=30,
    ) as client:
        processing_response = client.get(f"/v1/processing?limit={args.limit}")
        assert processing_response.status_code == 200, processing_response.text
        assert processing_response.headers["cache-control"] == "private, no-store"
        processing = processing_response.json()
        assert isinstance(processing, list)
        assert len(processing) <= args.limit
        assert all(item["stage"] in PROCESSING_STAGES for item in processing)
        assert all(item["attempt_count"] >= 0 for item in processing)
        assert all(
            item["stage"] != "complete" or item["retry_at"] is None
            for item in processing
        )

        purchase_response = client.get("/v1/purchases?limit=1")
        assert purchase_response.status_code == 200, purchase_response.text
        assert purchase_response.headers["cache-control"] == "private, no-store"
        purchases = purchase_response.json()
        assert isinstance(purchases, list)
        assert len(purchases) <= 1

    stages = Counter(item["stage"] for item in processing)
    unresolved = [
        {
            "capture_id": item["capture_id"],
            "stage": item["stage"],
            "captured_at": item["captured_at"],
            "attempt_count": item["attempt_count"],
            "latest_failure_code": item["latest_failure_code"],
        }
        for item in processing
        if item["stage"] != "complete"
    ]
    print(f"processing_records={len(processing)}")
    print(f"processing_stages={dict(sorted(stages.items()))}")
    print(f"unresolved_records={unresolved}")
    print(f"purchase_context={'available' if purchases else 'empty'}")
    print("unauthenticated_processing_status=401")
    print("private_no_store=true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 51))
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
