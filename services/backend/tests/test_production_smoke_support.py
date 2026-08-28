from __future__ import annotations

from typing import Any

import pytest

from foodlog_backend.ai_traces import trace_id_for_invocation
from scripts import production_smoke_support


def _trace_id(*, account_id: str, event_id: str, attempt: int) -> str:
    return trace_id_for_invocation(
        account_id=account_id,
        event_id=event_id,
        invocation_key=f"event:{event_id}:revision:1:attempt:{attempt}",
    )


def test_event_trace_correlation_ignores_unrelated_concurrent_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "account-owner"
    event_id = "event-cat"
    earlier = _trace_id(account_id=account_id, event_id=event_id, attempt=1)
    latest = _trace_id(account_id=account_id, event_id=event_id, attempt=2)
    unrelated = _trace_id(
        account_id=account_id,
        event_id="event-unrelated",
        attempt=1,
    )
    events: list[dict[str, Any]] = [
        {
            "action": "ai.trace_recorded",
            "subject_kind": "trace",
            "subject_id": unrelated,
            "created_at": "2026-08-28T03:00:03Z",
        },
        {
            "action": "ai.trace_recorded",
            "subject_kind": "trace",
            "subject_id": latest,
            "created_at": "2026-08-28T03:00:02Z",
        },
        {
            "action": "ai.trace_recorded",
            "subject_kind": "trace",
            "subject_id": earlier,
            "created_at": "2026-08-28T03:00:01Z",
        },
    ]
    monkeypatch.setattr(
        production_smoke_support,
        "request_json",
        lambda *_args, **_kwargs: events,
    )

    correlated = production_smoke_support._latest_event_trace_id(
        object(),  # type: ignore[arg-type]
        account_id=account_id,
        event_id=event_id,
        previous_trace_ids=set(),
    )

    assert correlated == latest


def test_event_trace_correlation_excludes_baseline_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "account-owner"
    event_id = "event-cat"
    trace_id = _trace_id(account_id=account_id, event_id=event_id, attempt=1)
    monkeypatch.setattr(
        production_smoke_support,
        "request_json",
        lambda *_args, **_kwargs: [
            {
                "action": "ai.trace_recorded",
                "subject_kind": "trace",
                "subject_id": trace_id,
                "created_at": "2026-08-28T03:00:01Z",
            }
        ],
    )

    correlated = production_smoke_support._latest_event_trace_id(
        object(),  # type: ignore[arg-type]
        account_id=account_id,
        event_id=event_id,
        previous_trace_ids={trace_id},
    )

    assert correlated is None
