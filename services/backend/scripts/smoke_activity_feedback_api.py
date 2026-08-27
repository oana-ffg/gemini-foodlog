from __future__ import annotations

import argparse
import os

import httpx


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

    unauthenticated = httpx.get(f"{args.api_url}/v1/activities", timeout=30)
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
        account_id = account_response.json()["id"]

        journal_response = client.get("/v1/journal")
        activities_response = client.get("/v1/activities")
        discarded_response = client.get("/v1/activities?status=not_cooking")
        invalid_status_response = client.get("/v1/activities?status=not-a-status")

        for response in (journal_response, activities_response, discarded_response):
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
        assert invalid_status_response.status_code == 422, invalid_status_response.text

        journal = journal_response.json()
        activities = activities_response.json()
        discarded = discarded_response.json()

        assert all(item["account_id"] == account_id for item in activities)
        assert all(item["status"] != "not_cooking" for item in journal)
        assert all(item["status"] == "not_cooking" for item in discarded)
        assert {item["id"] for item in activities} == {
            item["id"] for item in journal + discarded
        }

        revision_count = 0
        correction_count = 0
        for activity in activities:
            revision_response = client.get(f"/v1/meals/{activity['id']}/revisions")
            assert revision_response.status_code == 200, revision_response.text
            assert revision_response.headers["cache-control"] == "private, no-store"
            revisions = revision_response.json()
            assert revisions
            revision_count += len(revisions)
            correction_count += sum(
                revision.get("correction") is not None for revision in revisions
            )

    print(f"journal_records={len(journal)}")
    print(f"activity_records={len(activities)}")
    print(f"discarded_records={len(discarded)}")
    print(f"revision_records={revision_count}")
    print(f"targeted_corrections={correction_count}")
    print("tenant_scope=verified")
    print("private_no_store=true")
    print("unauthenticated_status=401")
    print("invalid_status=422")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
