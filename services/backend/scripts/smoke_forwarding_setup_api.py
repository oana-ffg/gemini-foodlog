from __future__ import annotations

import argparse
import os
import re

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

    unauthenticated = httpx.post(
        f"{args.api_url}/v1/inbound-mail-address",
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
        account_response = client.post("/v1/accounts")
        assert account_response.status_code == 200, account_response.text
        account_id = account_response.json()["id"]

        first_response = client.post("/v1/inbound-mail-address")
        second_response = client.post("/v1/inbound-mail-address")
        purchases_response = client.get("/v1/purchases?limit=50")

        for response in (first_response, second_response):
            assert response.status_code == 200, response.text
            assert "no-store" in response.headers["cache-control"]
        assert purchases_response.status_code == 200, purchases_response.text
        assert purchases_response.headers["cache-control"] == "private, no-store"

        first = first_response.json()
        second = second_response.json()
        purchases = purchases_response.json()
        assert first == second
        assert first["account_id"] == account_id
        assert first["id"] == "current"
        assert first["status"] == "active"
        assert re.fullmatch(
            rf"f-[0-9a-f]{{48}}@{re.escape(args.inbound_domain)}",
            first["address"],
        )

    print("stable_private_address=verified")
    print("opaque_address_shape=verified")
    print("tenant_scope=verified")
    print("private_no_store=true")
    print("unauthenticated_status=401")
    print(f"purchase_evidence_records={len(purchases)}")
    print(
        "setup_signal="
        + ("purchase_evidence_received" if purchases else "awaiting_first_purchase_email")
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--inbound-domain", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
