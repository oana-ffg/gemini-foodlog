from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime

import httpx

OVERSIZED_IMAGE_BYTES = (5 * 1024 * 1024) + 1


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

    with httpx.Client(
        base_url=args.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        timeout=60,
    ) as client:
        account_before_response = client.post("/v1/accounts")
        assert account_before_response.status_code == 200, account_before_response.text
        account_before = account_before_response.json()
        assert account_before["id"] != args.foreign_account_id

        cameras_response = client.get("/v1/cameras")
        assert cameras_response.status_code == 200, cameras_response.text
        own_camera = next(
            camera
            for camera in cameras_response.json()
            if camera["kind"] == "browser" and camera["status"] == "active"
        )

        foreign_image = client.get(
            f"/v1/captures/{args.foreign_capture_id}/image"
        )
        foreign_revoke = client.post(f"/v1/cameras/{args.foreign_camera_id}/revoke")
        assert foreign_image.status_code == 404, foreign_image.text
        assert foreign_revoke.status_code == 404, foreign_revoke.text

        metadata = {
            "schema_version": 1,
            "camera_id": own_camera["id"],
            "captured_at": datetime.now(UTC).isoformat(),
            "client_kind": "browser",
            "client_version": "foodlog-production-isolation-smoke/1",
            "sequence_id": "oversized-failure-smoke",
            "sequence_number": 1,
            "width": 1,
            "height": 1,
        }
        oversized = client.post(
            "/v1/captures",
            headers={"Idempotency-Key": "oversized-failure-smoke-v1"},
            data={"metadata": json.dumps(metadata)},
            files={
                "image": (
                    "oversized.png",
                    b"x" * OVERSIZED_IMAGE_BYTES,
                    "image/png",
                )
            },
        )
        assert oversized.status_code == 413, oversized.text

        account_after_response = client.post("/v1/accounts")
        assert account_after_response.status_code == 200, account_after_response.text
        account_after = account_after_response.json()

    assert account_after == account_before
    print(f"owner_account_id={account_before['id']}")
    print(f"foreign_account_id={args.foreign_account_id}")
    print("foreign_capture_read=404")
    print("foreign_camera_revoke=404")
    print("oversized_capture=413")
    print("owner_account_unchanged=true")
    print("model_calls=0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--foreign-account-id", required=True)
    parser.add_argument("--foreign-capture-id", required=True)
    parser.add_argument("--foreign-camera-id", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
