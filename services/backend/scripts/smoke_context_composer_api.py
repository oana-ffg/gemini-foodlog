from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from google.cloud.firestore_v1 import Client as FirestoreClient

SYNTHETIC_MARKER = "Synthetic context-composer smoke:"


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected_status: int,
    headers: dict[str, str] | None = None,
    json: dict[str, object] | None = None,
) -> object:
    response = client.request(method, path, headers=headers, json=json)
    assert response.status_code == expected_status, (
        f"{method} {path}: expected {expected_status}, got {response.status_code}: "
        f"{response.text}"
    )
    return response.json()


def retire_active_synthetic_records(client: httpx.Client) -> None:
    notes = request_json(
        client,
        "GET",
        "/v1/context-notes?include_inactive=true",
        expected_status=200,
    )
    assert isinstance(notes, list)
    for note in notes:
        if note["status"] == "active" and note["text"].startswith(SYNTHETIC_MARKER):
            request_json(
                client,
                "POST",
                f"/v1/context-notes/{note['id']}/retire",
                expected_status=200,
            )

    pages = request_json(
        client,
        "GET",
        "/v1/knowledge?include_retired=true",
        expected_status=200,
    )
    assert isinstance(pages, list)
    for page in pages:
        if page["lifecycle"] != "retired" and page["statement"].startswith(
            SYNTHETIC_MARKER
        ):
            request_json(
                client,
                "POST",
                f"/v1/knowledge/{page['id']}/retire",
                expected_status=200,
                headers={
                    "Idempotency-Key": f"context-smoke-recover-{page['id']}"
                },
                json={
                    "expected_revision_number": page["current_revision_number"],
                    "reason": "Recovered synthetic context-composer smoke cleanup.",
                },
            )


def smoke(args: argparse.Namespace) -> None:
    firestore = FirestoreClient(project=args.project)
    spend_ref = firestore.collection("system").document("model_spend")
    spend_before = spend_ref.get().to_dict()
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
    run_id = uuid4().hex
    local_now = datetime.now(ZoneInfo(args.timezone))
    tomorrow = (local_now + timedelta(days=1)).date()
    window_start = datetime.combine(
        tomorrow,
        datetime.min.time(),
        tzinfo=local_now.tzinfo,
    )
    window_end = window_start + timedelta(days=1)
    initial_text = (
        f"{SYNTHETIC_MARKER} my MIL brought duck, and we intend to cook it tomorrow "
        f"({run_id})."
    )
    edited_text = (
        f"{SYNTHETIC_MARKER} my MIL brought duck, and we now intend to cook it tomorrow "
        f"evening ({run_id})."
    )

    with httpx.Client(
        base_url=args.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        timeout=30,
    ) as client:
        account = request_json(client, "POST", "/v1/accounts", expected_status=200)
        assert isinstance(account, dict)
        retire_active_synthetic_records(client)

        create_key = f"context-composer-create-{run_id}"
        initial_payload = {
            "text": initial_text,
            "valid_from": window_start.isoformat(),
            "valid_until": window_end.isoformat(),
        }
        created = request_json(
            client,
            "POST",
            "/v1/context-notes",
            expected_status=201,
            headers={"Idempotency-Key": create_key},
            json=initial_payload,
        )
        exact_retry = request_json(
            client,
            "POST",
            "/v1/context-notes",
            expected_status=201,
            headers={"Idempotency-Key": create_key},
            json=initial_payload,
        )
        changed_retry = request_json(
            client,
            "POST",
            "/v1/context-notes",
            expected_status=409,
            headers={"Idempotency-Key": create_key},
            json={**initial_payload, "text": edited_text},
        )
        assert exact_retry == created, {
            key: (created.get(key), exact_retry.get(key))
            for key in set(created) | set(exact_retry)
            if created.get(key) != exact_retry.get(key)
        }
        assert changed_retry == {
            "detail": "idempotency_key_reused_with_different_payload"
        }
        assert isinstance(created, dict)
        initial_id = created["id"]
        assert created["text"] == initial_text
        assert created["status"] == "active"
        assert datetime.fromisoformat(created["valid_from"]) == window_start
        assert datetime.fromisoformat(created["valid_until"]) == window_end

        replacement_key = f"context-composer-edit-{run_id}"
        replacement_payload = {
            "text": edited_text,
            "valid_from": window_start.isoformat(),
            "valid_until": window_end.isoformat(),
        }
        replacement = request_json(
            client,
            "POST",
            "/v1/context-notes",
            expected_status=201,
            headers={"Idempotency-Key": replacement_key},
            json=replacement_payload,
        )
        replacement_retry = request_json(
            client,
            "POST",
            "/v1/context-notes",
            expected_status=201,
            headers={"Idempotency-Key": replacement_key},
            json=replacement_payload,
        )
        assert replacement_retry == replacement
        assert isinstance(replacement, dict)
        replacement_id = replacement["id"]
        retired_initial = request_json(
            client,
            "POST",
            f"/v1/context-notes/{initial_id}/retire",
            expected_status=200,
        )
        assert isinstance(retired_initial, dict)
        assert retired_initial["status"] == "retired"

        promote_key = f"context-composer-promote-{run_id}"
        promoted = request_json(
            client,
            "POST",
            "/v1/knowledge",
            expected_status=201,
            headers={"Idempotency-Key": promote_key},
            json={"statement": edited_text},
        )
        promoted_retry = request_json(
            client,
            "POST",
            "/v1/knowledge",
            expected_status=201,
            headers={"Idempotency-Key": promote_key},
            json={"statement": edited_text},
        )
        assert promoted_retry == promoted
        assert isinstance(promoted, dict)
        page = promoted["page"]
        assert page["statement"] == edited_text
        assert page["lifecycle"] == "confirmed"
        assert promoted["source_note"]["status"] == "retired"

        retired_replacement = request_json(
            client,
            "POST",
            f"/v1/context-notes/{replacement_id}/retire",
            expected_status=200,
        )
        assert isinstance(retired_replacement, dict)
        assert retired_replacement["status"] == "retired"

        active_notes = request_json(
            client,
            "GET",
            "/v1/context-notes",
            expected_status=200,
        )
        all_notes = request_json(
            client,
            "GET",
            "/v1/context-notes?include_inactive=true",
            expected_status=200,
        )
        assert isinstance(active_notes, list) and isinstance(all_notes, list)
        active_ids = {note["id"] for note in active_notes}
        assert initial_id not in active_ids and replacement_id not in active_ids
        by_id = {note["id"]: note for note in all_notes}
        assert by_id[initial_id]["text"] == initial_text
        assert by_id[replacement_id]["text"] == edited_text
        assert by_id[initial_id]["status"] == by_id[replacement_id]["status"] == (
            "retired"
        )

        history = request_json(
            client,
            "GET",
            f"/v1/knowledge/{page['id']}",
            expected_status=200,
        )
        assert isinstance(history, dict)
        assert history["page"]["statement"] == edited_text
        assert history["revisions"][-1]["source"] == "user_statement"

        retired_page = request_json(
            client,
            "POST",
            f"/v1/knowledge/{page['id']}/retire",
            expected_status=200,
            headers={"Idempotency-Key": f"context-composer-cleanup-{run_id}"},
            json={
                "expected_revision_number": page["current_revision_number"],
                "reason": "Synthetic context-composer smoke cleanup.",
            },
        )
        assert isinstance(retired_page, dict)
        assert retired_page["page"]["lifecycle"] == "retired"

    spend_after = spend_ref.get().to_dict()
    assert spend_after == spend_before

    print(f"account_id={account['id']}")
    print(f"initial_note_id={initial_id}")
    print(f"replacement_note_id={replacement_id}")
    print(f"promoted_page_id={page['id']}")
    print(f"local_window_offset={window_start.utcoffset()}")
    print("exact_create_and_edit_retries=true")
    print("changed_create_retry_rejected=true")
    print("edit_preserved_retired_original=true")
    print("promotion_preserved_exact_statement=true")
    print("synthetic_context_and_knowledge_retired=true")
    print("model_spend_ledger_unchanged=true")
    print("model_calls=0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--timezone", default="Europe/Copenhagen")
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
