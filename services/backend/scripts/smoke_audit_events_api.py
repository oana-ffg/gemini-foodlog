from __future__ import annotations

import argparse
import os

import httpx

ALLOWED_EVENT_FIELDS = {
    "schema_version",
    "id",
    "account_id",
    "action",
    "actor_kind",
    "source",
    "subject_kind",
    "subject_id",
    "created_at",
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

    unauthenticated = httpx.get(f"{args.api_url}/v1/audit-events", timeout=30)
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
        account_response = client.post("/v1/accounts")
        assert account_response.status_code == 200, account_response.text
        account = account_response.json()

        processing_response = client.get("/v1/processing?limit=1")
        assert processing_response.status_code == 200, processing_response.text
        processing = processing_response.json()
        assert processing, "the production test account has no retained capture"
        capture_id = processing[0]["capture_id"]

        first_image = client.get(f"/v1/captures/{capture_id}/image")
        second_image = client.get(f"/v1/captures/{capture_id}/image")
        assert first_image.status_code == second_image.status_code == 200
        assert first_image.content == second_image.content

        events_response = client.get("/v1/audit-events?limit=200")
        assert events_response.status_code == 200, events_response.text
        assert events_response.headers["cache-control"] == "private, no-store"
        events = events_response.json()

    assert events
    assert all(set(event) == ALLOWED_EVENT_FIELDS for event in events)
    assert all(event["account_id"] == account["id"] for event in events)
    assert (
        sum(
            event["action"] == "account.provisioned"
            and event["subject_id"] == account["id"]
            for event in events
        )
        == 1
    )
    assert (
        sum(
            event["action"] == "capture.image_read"
            and event["subject_id"] == capture_id
            for event in events
        )
        == 1
    )
    assert len({event["id"] for event in events}) == len(events)

    print(f"audit_records={len(events)}")
    print(f"actions={sorted({event['action'] for event in events})}")
    print("account_provisioned_evidence=1")
    print("repeated_image_read_evidence=1")
    print("tenant_scope=verified")
    print("private_no_store=true")
    print("unauthenticated_status=401")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
