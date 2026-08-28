from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
from production_smoke_support import request_json, trace_ids, upload_fixture, wait_for_activity


def smoke(args: argparse.Namespace) -> None:
    fixture_sha256 = sha256(args.fixture.read_bytes()).hexdigest()
    assert fixture_sha256 == args.fixture_sha256, (
        f"fixture hash mismatch: expected {args.fixture_sha256}, got {fixture_sha256}"
    )

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
    camera_id: str | None = None

    with httpx.Client(
        base_url=args.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        timeout=60,
    ) as client:
        account = request_json(client, "POST", "/v1/accounts", expected_status=200)
        assert isinstance(account, dict)
        account_id = account["id"]

        try:
            if args.resume_capture_id is not None:
                captures = request_json(
                    client,
                    "GET",
                    "/v1/captures?limit=200",
                    expected_status=200,
                )
                assert isinstance(captures, list)
                resumed = next(
                    (
                        item
                        for item in captures
                        if item.get("id") == args.resume_capture_id
                    ),
                    None,
                )
                assert isinstance(resumed, dict), "resume capture is not visible"
                assert resumed.get("content_sha256") == fixture_sha256
                capture_id = args.resume_capture_id
                baseline_trace_ids: set[str] = set()
            else:
                issued = request_json(
                    client,
                    "POST",
                    "/v1/device-cameras",
                    expected_status=200,
                    json_body={"name": f"Cat negative smoke {run_id[:8]}"},
                )
                assert isinstance(issued, dict)
                camera_id = issued["camera"]["id"]
                baseline_trace_ids = trace_ids(client)
                capture_id = upload_fixture(
                    api_url=args.api_url,
                    camera_id=camera_id,
                    credential=issued["credential"],
                    fixture=args.fixture,
                    captured_at=datetime.now(UTC) - timedelta(days=58),
                    client_version="cat-not-cooking-smoke/1",
                    sequence_id=f"cat-negative-{run_id}",
                    sequence_number=0,
                    idempotency_key=f"cat-negative-{run_id}",
                )
            activity, trace_id = wait_for_activity(
                client,
                account_id=account_id,
                capture_id=capture_id,
                previous_trace_ids=baseline_trace_ids,
                timeout_seconds=args.timeout_seconds,
            )
            hypothesis = activity.get("activity_hypothesis")
            assert isinstance(hypothesis, dict)
            assert hypothesis.get("kind") == "likely_non_cooking", hypothesis
            assert hypothesis.get("best_guess")
            assert set(hypothesis.get("allowed_actions", [])) == {
                "correct",
                "discard_not_cooking",
            }
            assert activity["status"] == "provisional"

            journal_before = request_json(client, "GET", "/v1/journal", expected_status=200)
            assert isinstance(journal_before, list)
            assert activity["id"] in {item["id"] for item in journal_before}

            feedback_key = f"cat-not-cooking-{run_id}"
            feedback_payload = {
                "kind": "not_cooking",
                "explanation": (
                    "Synthetic negative fixture: the cat jumped onto the counter and nobody "
                    "was preparing food."
                ),
            }
            feedback = request_json(
                client,
                "POST",
                f"/v1/meals/{activity['id']}/feedback",
                expected_status=200,
                headers={"Idempotency-Key": feedback_key},
                json_body=feedback_payload,
            )
            feedback_retry = request_json(
                client,
                "POST",
                f"/v1/meals/{activity['id']}/feedback",
                expected_status=200,
                headers={"Idempotency-Key": feedback_key},
                json_body=feedback_payload,
            )
            assert feedback_retry == feedback
            assert isinstance(feedback, dict)
            assert feedback["learning_outcome"] == "not_cooking"
            assert feedback["knowledge"] is None
            assert feedback["revision"]["status"] == "not_cooking"
            assert feedback["revision"]["number"] == activity["revision_number"] + 1
            assert feedback["revision"]["inference"]["title"] == activity["title"]

            journal_after = request_json(client, "GET", "/v1/journal", expected_status=200)
            assert isinstance(journal_after, list)
            assert activity["id"] not in {item["id"] for item in journal_after}

            discarded = request_json(
                client,
                "GET",
                "/v1/activities?status=not_cooking",
                expected_status=200,
            )
            assert isinstance(discarded, list)
            retained = next(item for item in discarded if item["id"] == activity["id"])
            assert retained["status"] == "not_cooking"
            assert retained["revision_number"] == feedback["revision"]["number"]

            revisions = request_json(
                client,
                "GET",
                f"/v1/meals/{activity['id']}/revisions",
                expected_status=200,
            )
            assert isinstance(revisions, list)
            assert [item["number"] for item in revisions] == [1, 2]
            assert [item["status"] for item in revisions] == [
                "provisional",
                "not_cooking",
            ]
            assert [item["source"] for item in revisions] == [
                "inference",
                "user_feedback",
            ]
            assert revisions[0]["inference"] == revisions[1]["inference"]

            print(f"account_id={account_id}")
            print(f"fixture_sha256={fixture_sha256}")
            print(f"capture_id={capture_id}")
            print(f"event_id={activity['event_id']}")
            print(f"meal_id={activity['id']}")
            print(f"trace_id={trace_id}")
            print(f"model_kind={hypothesis['kind']}")
            print(f"model_best_guess={hypothesis['best_guess']}")
            print(f"feedback_id={feedback['feedback']['id']}")
            print(f"not_cooking_revision_id={feedback['revision']['id']}")
            print("idempotent_retry=true")
            print("journal_exclusion=true")
            print("discarded_history_retained=true")
        finally:
            if camera_id is not None:
                revoked = request_json(
                    client,
                    "POST",
                    f"/v1/device-cameras/{camera_id}/revoke",
                    expected_status=200,
                )
                assert isinstance(revoked, dict) and revoked["status"] == "revoked"
                print("temporary_camera_revoked=true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--fixture-sha256", required=True)
    parser.add_argument("--resume-capture-id")
    parser.add_argument("--timeout-seconds", type=int, default=240, choices=range(30, 601))
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
