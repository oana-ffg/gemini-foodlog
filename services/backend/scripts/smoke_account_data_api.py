from __future__ import annotations

import argparse
import os
from collections.abc import Iterable
from typing import Any

import httpx

FORBIDDEN_RESPONSE_KEYS = frozenset(
    {
        "client_instance_id_hash",
        "credential",
        "credential_hash",
        "idempotency_hash",
        "idempotency_key",
        "object_key",
    }
)


def _walk_json(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _assert_safe_tenant_data(value: Any, account_id: str) -> None:
    for key, child in _walk_json(value):
        assert key not in FORBIDDEN_RESPONSE_KEYS, f"sensitive response key: {key}"
        if key == "account_id":
            assert child == account_id, f"foreign account data: {child}"


def _private_json(client: httpx.Client, path: str) -> Any:
    response = client.get(path)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
    assert response.headers["cache-control"] == "private, no-store", path
    return response.json()


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

    for path in ("/v1/captures", "/v1/feedback"):
        unauthenticated = httpx.get(f"{args.api_url}{path}", timeout=30)
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
        account_id = account["id"]

        top_level = {
            "consents": _private_json(client, "/v1/consents"),
            "cameras": _private_json(client, "/v1/cameras"),
            "captures": _private_json(client, "/v1/captures?limit=200"),
            "activities": _private_json(client, "/v1/activities"),
            "feedback": _private_json(client, "/v1/feedback?limit=200"),
            "open_questions": _private_json(
                client, "/v1/questions?question_status=open"
            ),
            "answered_questions": _private_json(
                client, "/v1/questions?question_status=answered"
            ),
            "superseded_questions": _private_json(
                client, "/v1/questions?question_status=superseded"
            ),
            "context_notes": _private_json(
                client, "/v1/context-notes?include_inactive=true"
            ),
            "knowledge": _private_json(
                client, "/v1/knowledge?include_retired=true&limit=100"
            ),
            "purchases": _private_json(client, "/v1/purchases?limit=50"),
            "audit_events": _private_json(client, "/v1/audit-events?limit=200"),
        }

        revisions = [
            revision
            for activity in top_level["activities"]
            for revision in _private_json(
                client, f"/v1/meals/{activity['id']}/revisions"
            )
        ]
        knowledge_histories = [
            _private_json(client, f"/v1/knowledge/{page['id']}")
            for page in top_level["knowledge"]
        ]
        purchase_details = [
            _private_json(client, f"/v1/purchases/{purchase['id']}")
            for purchase in top_level["purchases"]
        ]

        complete_inventory = {
            "account": account,
            **top_level,
            "meal_revisions": revisions,
            "knowledge_histories": knowledge_histories,
            "purchase_details": purchase_details,
        }
        _assert_safe_tenant_data(complete_inventory, account_id)

        captures = top_level["captures"]
        assert len(captures) == account["accepted_image_count"], (
            "capture inventory does not match the accepted-image ledger: "
            f"{len(captures)} != {account['accepted_image_count']}"
        )

    feedback = top_level["feedback"]
    documents = [
        document
        for purchase in purchase_details
        for document in purchase["documents"]
    ]
    items = [item for document in documents for item in document["items"]]
    charges = [charge for document in documents for charge in document["charges"]]
    knowledge_revision_count = sum(
        len(history["revisions"]) for history in knowledge_histories
    )

    print(
        "accepted_images="
        f"{account['accepted_image_count']}/{account['trial_image_limit']}"
    )
    print(f"cameras={len(top_level['cameras'])}")
    print(f"captures={len(captures)}")
    print(f"activities={len(top_level['activities'])}")
    print(f"meal_revisions={len(revisions)}")
    print(f"open_questions={len(top_level['open_questions'])}")
    print(f"answered_questions={len(top_level['answered_questions'])}")
    print(f"superseded_questions={len(top_level['superseded_questions'])}")
    print(f"meal_feedback={len(feedback['meal_feedback'])}")
    print(f"question_responses={len(feedback['question_responses'])}")
    print(f"context_notes={len(top_level['context_notes'])}")
    print(f"knowledge_pages={len(top_level['knowledge'])}")
    print(f"knowledge_revisions={knowledge_revision_count}")
    print(f"purchases={len(purchase_details)}")
    print(f"purchase_documents={len(documents)}")
    print(f"purchase_items={len(items)}")
    print(f"purchase_charges={len(charges)}")
    print(f"audit_events={len(top_level['audit_events'])}")
    print("capture_ledger=verified")
    print("tenant_scope=verified")
    print("sensitive_fields=absent")
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
