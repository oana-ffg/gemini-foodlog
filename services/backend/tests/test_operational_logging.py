from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from foodlog_backend.operational_logging import (
    emit_operational_event,
    install_request_logging,
    safe_error_kind,
    trace_id_from_header,
)


def payloads(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line]


def test_structured_events_reject_arbitrary_fields_and_never_render_exception_text(
    capfd: pytest.CaptureFixture[str],
) -> None:
    secret = "Bearer private-token and private prompt text"

    emit_operational_event(
        "ERROR",
        "example_failed",
        event_id="event-123",
        error_kind=safe_error_kind(RuntimeError(secret)),
    )
    output = capfd.readouterr().out

    assert payloads(output) == [
        {
            "schema": "foodlog_operational_event_v1",
            "severity": "ERROR",
            "event": "example_failed",
            "event_id": "event-123",
            "error_kind": "RuntimeError",
        }
    ]
    assert secret not in output
    with pytest.raises(ValueError, match="unsupported operational log fields"):
        emit_operational_event("INFO", "unsafe", authorization=secret)
    emit_operational_event("WARNING", "unsafe_value_redacted", outcome=secret)
    [redacted] = payloads(capfd.readouterr().out)
    assert str(redacted["outcome"]).startswith("sha256:")
    assert secret not in json.dumps(redacted)


def test_trace_parser_accepts_only_cloud_trace_identity() -> None:
    trace_id = "a" * 32
    assert trace_id_from_header(f"{trace_id}/123;o=1") == trace_id
    assert trace_id_from_header("Bearer should-not-be-logged") is None


def test_request_logging_uses_route_templates_and_ignores_headers_and_bodies(
    capfd: pytest.CaptureFixture[str],
) -> None:
    app = FastAPI()
    install_request_logging(app, service="test_api", environment="test")

    @app.post("/v1/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    secret = "private-token-private-body"
    trace_id = "b" * 32
    with TestClient(app) as client:
        response = client.post(
            "/v1/items/item-123",
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Cloud-Trace-Context": f"{trace_id}/42;o=1",
            },
            json={"prompt": secret},
        )

    assert response.status_code == 200
    assert len(response.headers["x-request-id"]) == 32
    output = capfd.readouterr().out
    [payload] = payloads(output)
    assert payload["http_route"] == "/v1/items/{item_id}"
    assert payload["http_status"] == 200
    assert payload["trace_id"] == trace_id
    assert secret not in output
    assert "item-123" not in output
