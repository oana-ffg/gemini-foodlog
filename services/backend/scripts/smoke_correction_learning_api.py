from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected_status: int,
    headers: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
) -> object:
    response = client.request(method, path, headers=headers, json=json_body)
    assert response.status_code == expected_status, (
        f"{method} {path}: expected {expected_status}, got {response.status_code}: "
        f"{response.text}"
    )
    return response.json()


def trace_ids(client: httpx.Client) -> set[str]:
    events = request_json(
        client,
        "GET",
        "/v1/audit-events?limit=200",
        expected_status=200,
    )
    assert isinstance(events, list)
    return {
        event["subject_id"]
        for event in events
        if event.get("action") == "ai.trace_recorded"
        and event.get("subject_kind") == "trace"
        and isinstance(event.get("subject_id"), str)
    }


def wait_for_activity(
    client: httpx.Client,
    *,
    capture_id: str,
    previous_trace_ids: set[str],
    timeout_seconds: int,
) -> tuple[dict[str, object], str]:
    deadline = time.monotonic() + timeout_seconds
    last_stage = "not_visible"
    while time.monotonic() < deadline:
        captures = request_json(
            client,
            "GET",
            "/v1/captures?limit=200",
            expected_status=200,
        )
        assert isinstance(captures, list)
        capture = next(
            (item for item in captures if item.get("id") == capture_id),
            None,
        )
        event_id = capture.get("event_id") if isinstance(capture, dict) else None
        if isinstance(event_id, str):
            activities = request_json(client, "GET", "/v1/activities", expected_status=200)
            assert isinstance(activities, list)
            activity = next(
                (item for item in activities if item.get("event_id") == event_id),
                None,
            )
            if isinstance(activity, dict):
                new_trace_ids = trace_ids(client) - previous_trace_ids
                if len(new_trace_ids) == 1:
                    return activity, new_trace_ids.pop()
                if len(new_trace_ids) > 1:
                    raise AssertionError(
                        "more than one new AI trace appeared in the test account"
                    )

        processing = request_json(
            client,
            "GET",
            "/v1/processing?limit=50",
            expected_status=200,
        )
        assert isinstance(processing, list)
        status = next(
            (item for item in processing if item.get("capture_id") == capture_id),
            None,
        )
        if isinstance(status, dict):
            last_stage = str(status.get("stage"))
            if last_stage in {"attention_required", "evaluation_complete"}:
                raise AssertionError(
                    f"capture {capture_id} reached terminal stage {last_stage} without an activity"
                )
        time.sleep(3)
    raise AssertionError(
        f"capture {capture_id} did not publish an activity within {timeout_seconds}s "
        f"(last stage: {last_stage})"
    )


def upload_fixture(
    *,
    api_url: str,
    camera_id: str,
    credential: str,
    fixture: Path,
    captured_at: datetime,
    run_id: str,
    sequence_number: int,
) -> str:
    image_bytes = fixture.read_bytes()
    with Image.open(fixture) as image:
        width, height = image.size
    metadata = {
        "schema_version": 1,
        "camera_id": camera_id,
        "captured_at": captured_at.isoformat(),
        "client_kind": "simulator",
        "client_version": "north-star-correction-smoke/1",
        "sequence_id": f"north-star-{run_id}",
        "sequence_number": sequence_number,
        "width": width,
        "height": height,
    }
    response = httpx.post(
        f"{api_url}/v1/captures",
        headers={
            "Authorization": f"FoodLogCamera {credential}",
            "Idempotency-Key": f"north-star-{run_id}-{sequence_number}",
        },
        files={
            "metadata": (
                None,
                json.dumps(metadata, separators=(",", ":")),
                "application/json",
            ),
            "image": (fixture.name, image_bytes, "image/png"),
        },
        timeout=60,
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["duplicate"] is False
    return payload["capture_id"]


def smoke(args: argparse.Namespace) -> None:
    fixture_bytes = args.fixture.read_bytes()
    fixture_sha256 = sha256(fixture_bytes).hexdigest()
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
    camera_ids: list[str] = []
    page_id: str | None = None

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
            first_camera = request_json(
                client,
                "POST",
                "/v1/device-cameras",
                expected_status=200,
                json_body={"name": f"North-star first event {run_id[:8]}"},
            )
            assert isinstance(first_camera, dict)
            first_camera_id = first_camera["camera"]["id"]
            camera_ids.append(first_camera_id)
            first_trace_baseline = trace_ids(client)
            first_capture_id = upload_fixture(
                api_url=args.api_url,
                camera_id=first_camera_id,
                credential=first_camera["credential"],
                fixture=args.fixture,
                captured_at=datetime.now(UTC) - timedelta(days=60),
                run_id=run_id,
                sequence_number=0,
            )
            first_activity, first_trace_id = wait_for_activity(
                client,
                capture_id=first_capture_id,
                previous_trace_ids=first_trace_baseline,
                timeout_seconds=args.timeout_seconds,
            )
            first_hypothesis = first_activity.get("activity_hypothesis")
            assert isinstance(first_hypothesis, dict)
            assert first_hypothesis.get("kind") == "tentative_meal"
            first_guess = str(first_hypothesis.get("best_guess") or "")
            assert args.expected_label_term.casefold() not in first_guess.casefold(), (
                "the first inference already contained the learned label, so this run cannot "
                "demonstrate a before/after classification change"
            )

            feedback = request_json(
                client,
                "POST",
                f"/v1/meals/{first_activity['id']}/feedback",
                expected_status=200,
                headers={"Idempotency-Key": f"north-star-feedback-{run_id}"},
                json_body={
                    "kind": "correct",
                    "actual_meal": args.corrected_meal,
                    "explanation": args.learning_statement,
                    "learning_disposition": "reusable",
                },
            )
            assert isinstance(feedback, dict)
            assert feedback["learning_outcome"] == "knowledge_applied"
            assert feedback["revision"]["number"] == first_activity["revision_number"] + 1
            knowledge = feedback["knowledge"]
            page_id = knowledge["page"]["id"]
            knowledge_revision_id = knowledge["revision"]["id"]
            assert knowledge["revision"]["number"] == 1
            assert knowledge["revision"]["statement"] == args.learning_statement

            second_camera = request_json(
                client,
                "POST",
                "/v1/device-cameras",
                expected_status=200,
                json_body={"name": f"North-star learned event {run_id[:8]}"},
            )
            assert isinstance(second_camera, dict)
            second_camera_id = second_camera["camera"]["id"]
            camera_ids.append(second_camera_id)
            second_trace_baseline = trace_ids(client)
            second_capture_id = upload_fixture(
                api_url=args.api_url,
                camera_id=second_camera_id,
                credential=second_camera["credential"],
                fixture=args.fixture,
                captured_at=datetime.now(UTC) - timedelta(days=59),
                run_id=run_id,
                sequence_number=1,
            )
            second_activity, second_trace_id = wait_for_activity(
                client,
                capture_id=second_capture_id,
                previous_trace_ids=second_trace_baseline,
                timeout_seconds=args.timeout_seconds,
            )
            second_hypothesis = second_activity.get("activity_hypothesis")
            assert isinstance(second_hypothesis, dict)
            assert second_activity["event_id"] != first_activity["event_id"]
            second_guess = str(second_hypothesis.get("best_guess") or "")
            assert args.expected_label_term.casefold() in second_guess.casefold(), (
                f"later best guess did not contain {args.expected_label_term!r}: {second_guess!r}"
            )
            contextual_revision_ids = {
                item["source_id"]
                for item in second_hypothesis.get("contextual_evidence", [])
                if item.get("source_kind") == "household_knowledge"
            }
            assumption_revision_ids = {
                item["knowledge_revision_id"]
                for item in second_hypothesis.get("assumptions", [])
                if isinstance(item.get("knowledge_revision_id"), str)
            }
            assert knowledge_revision_id in contextual_revision_ids
            assert knowledge_revision_id in assumption_revision_ids

            print(f"account_id={account_id}")
            print(f"fixture_sha256={fixture_sha256}")
            print(f"first_capture_id={first_capture_id}")
            print(f"first_event_id={first_activity['event_id']}")
            print(f"first_meal_id={first_activity['id']}")
            print(f"first_trace_id={first_trace_id}")
            print(f"first_best_guess={first_guess}")
            print(f"feedback_id={feedback['feedback']['id']}")
            print(f"correction_revision_id={feedback['revision']['id']}")
            print(f"knowledge_page_id={page_id}")
            print(f"knowledge_revision_id={knowledge_revision_id}")
            print(f"second_capture_id={second_capture_id}")
            print(f"second_event_id={second_activity['event_id']}")
            print(f"second_meal_id={second_activity['id']}")
            print(f"second_trace_id={second_trace_id}")
            print(f"second_best_guess={second_guess}")
            print("knowledge_context_cited=true")
            print("knowledge_assumption_cited=true")
        finally:
            if page_id is not None:
                request_json(
                    client,
                    "POST",
                    f"/v1/knowledge/{page_id}/retire",
                    expected_status=200,
                    headers={"Idempotency-Key": f"north-star-retire-{run_id}"},
                    json_body={
                        "expected_revision_number": 1,
                        "reason": (
                            "Completed the synthetic north-star production proof; preserve "
                            "history but exclude this fixture-only rule from future use."
                        ),
                    },
                )
                print("synthetic_knowledge_retired=true")
            for camera_id in camera_ids:
                revoked = request_json(
                    client,
                    "POST",
                    f"/v1/device-cameras/{camera_id}/revoke",
                    expected_status=200,
                )
                assert isinstance(revoked, dict) and revoked["status"] == "revoked"
            print(f"temporary_cameras_revoked={len(camera_ids)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--fixture-sha256", required=True)
    parser.add_argument("--corrected-meal", required=True)
    parser.add_argument("--learning-statement", required=True)
    parser.add_argument("--expected-label-term", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=240, choices=range(30, 601))
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
