from __future__ import annotations

import json
import time
from datetime import datetime
from hashlib import sha256
from pathlib import Path

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


def _event_trace_ids(*, account_id: str, event_id: str) -> set[str]:
    """Return the bounded trace identities an ordinary event run can produce."""
    trace_ids: set[str] = set()
    for revision in range(1, 21):
        for attempt in range(1, 21):
            root_key = f"event:{event_id}:revision:{revision}:attempt:{attempt}"
            for invocation_key in (root_key, f"{root_key}:repair:1"):
                identity = (
                    f"application-visible-ai-trace-v1\0{account_id}\0{event_id}\0"
                    f"{invocation_key}"
                )
                trace_ids.add(f"trace-{sha256(identity.encode()).hexdigest()}")
    return trace_ids


def _latest_event_trace_id(
    client: httpx.Client,
    *,
    account_id: str,
    event_id: str,
    previous_trace_ids: set[str],
) -> str | None:
    expected_ids = _event_trace_ids(account_id=account_id, event_id=event_id)
    events = request_json(
        client,
        "GET",
        "/v1/audit-events?limit=200",
        expected_status=200,
    )
    assert isinstance(events, list)
    candidates = [
        event
        for event in events
        if event.get("action") == "ai.trace_recorded"
        and event.get("subject_kind") == "trace"
        and event.get("subject_id") in expected_ids
        and event.get("subject_id") not in previous_trace_ids
        and isinstance(event.get("created_at"), str)
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda event: event["created_at"])
    trace_id = latest.get("subject_id")
    assert isinstance(trace_id, str)
    return trace_id


def wait_for_activity(
    client: httpx.Client,
    *,
    account_id: str,
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
                trace_id = _latest_event_trace_id(
                    client,
                    account_id=account_id,
                    event_id=event_id,
                    previous_trace_ids=previous_trace_ids,
                )
                if trace_id is not None:
                    return activity, trace_id

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
    client_version: str,
    sequence_id: str,
    sequence_number: int,
    idempotency_key: str,
) -> str:
    image_bytes = fixture.read_bytes()
    with Image.open(fixture) as image:
        width, height = image.size
    metadata = {
        "schema_version": 1,
        "camera_id": camera_id,
        "captured_at": captured_at.isoformat(),
        "client_kind": "simulator",
        "client_version": client_version,
        "sequence_id": sequence_id,
        "sequence_number": sequence_number,
        "width": width,
        "height": height,
    }
    response = httpx.post(
        f"{api_url}/v1/captures",
        headers={
            "Authorization": f"FoodLogCamera {credential}",
            "Idempotency-Key": idempotency_key,
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
