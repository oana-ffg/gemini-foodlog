from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from foodlog_backend.ai_traces import (
    AiTraceCapture,
    AiTraceIntegrityError,
    AiTraceService,
    audit_application_visible_trace,
)
from foodlog_backend.errors import AiTraceNotFound
from foodlog_backend.model_accounting import ModelInvocationSpec, reservation_id_for_invocation
from foodlog_backend.models import ModelUsageRecord, utc_now
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.storage import InMemoryObjectStore


def _spec(*, account_id: str, invocation_key: str = "event-attempt-1") -> ModelInvocationSpec:
    return ModelInvocationSpec(
        invocation_key=invocation_key,
        account_id=account_id,
        event_id="trace-event-1",
        model="gemini-3.6-flash",
        region="eu",
        purpose="event_inference",
        prompt_version="food-event-v5",
        max_prompt_tokens=1_000,
        max_output_tokens=100,
        max_billable_calls=3,
        retry_attempt=0,
        evaluation=False,
    )


def _usage(spec: ModelInvocationSpec, *, succeeded: bool = True) -> ModelUsageRecord:
    return ModelUsageRecord(
        id=reservation_id_for_invocation(spec),
        reservation_id=reservation_id_for_invocation(spec),
        account_id=spec.account_id,
        event_id=spec.event_id,
        invocation_id="provider-invocation-1",
        model=spec.model,
        model_version="gemini-3.6-flash-001",
        region=spec.region,
        prompt_version=spec.prompt_version,
        purpose=spec.purpose,
        retry_attempt=spec.retry_attempt,
        evaluation=spec.evaluation,
        outcome="succeeded" if succeeded else "failed",
        prompt_tokens=40 if succeeded else 0,
        response_tokens=20 if succeeded else 0,
        thinking_tokens=5 if succeeded else 0,
        total_tokens=65 if succeeded else 0,
        actual_usd_nanos=40_000 if succeeded else 0,
        actual_dkk_micros=320 if succeeded else 0,
        reserved_dkk_micros=10_000,
        error_code=None if succeeded else "InvalidModelOutputError",
    )


def test_trace_round_trip_is_hashed_redacted_and_account_scoped() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("trace-owner")
        foreign = await repository.provision_account("trace-foreign-owner")
        store = InMemoryObjectStore()
        service = AiTraceService(repository=repository, object_store=store)
        spec = _spec(account_id=account.id)
        started_at = utc_now()
        capture = AiTraceCapture(
            spec=spec,
            request={
                "model": spec.model,
                "system_instruction": "Classify the event.",
                "user_content": {
                    "event_id": spec.event_id,
                    "context": {"user_notes": [{"note": "Duck may be cooked tomorrow."}]},
                },
                "response_schema": {"type": "object"},
                "tools": [{"name": "get_current_event_evidence"}],
                "run_config": {"max_llm_calls": 3},
                "authorization": "Bearer request-secret",
                "api_key": "AIzaABCDEFGHIJKLMNOPQRSTUVWXY1234",
            },
            started_at=started_at,
        )
        capture.record_event(
            {
                "author": "food_event_reasoner",
                "content": {
                    "parts": [
                        {
                            "function_call": {
                                "name": "get_current_event_evidence",
                                "args": {"event_id": spec.event_id},
                            }
                        },
                        {"thought": True, "text": "hidden internal reasoning"},
                    ]
                },
                "actions": {"requested_auth_configs": {"token": "tool-secret"}},
            }
        )
        capture.record_event(
            {
                "author": "get_current_event_evidence",
                "content": {
                    "parts": [
                        {
                            "function_response": {
                                "name": "get_current_event_evidence",
                                "response": {
                                    "capture_ids": ["capture-1"],
                                    "inline_image": b"private-image-bytes",
                                },
                            }
                        }
                    ]
                },
            }
        )
        completed_at = started_at + timedelta(milliseconds=432)
        record = await service.persist(
            capture=capture,
            usage=_usage(spec),
            response={
                "best_guess": "Air-fried steak",
                "chain_of_thought": "hidden response reasoning",
            },
            completed_at=completed_at,
        )
        retry = await service.persist(
            capture=capture,
            usage=_usage(spec),
            response={
                "best_guess": "Air-fried steak",
                "chain_of_thought": "hidden response reasoning",
            },
            completed_at=completed_at,
        )
        assert retry == record
        assert record.latency_ms == 432
        assert record.object_key == f"accounts/{account.id}/traces/{record.id}.json.gz"

        payload = await service.read(account_id=account.id, trace_id=record.id)
        audit_result = audit_application_visible_trace(payload)
        assert audit_result == {
            "event_count": 2,
            "tool_call_count": 1,
            "tool_response_count": 1,
            "binary_reference_count": 1,
            "redaction_verified": True,
        }
        serialized = json.dumps(payload, sort_keys=True)
        assert payload["request"]["user_content"]["context"]["user_notes"][0]["note"] == (
            "Duck may be cooked tomorrow."
        )
        assert payload["events"][0]["content"]["parts"][1] == {"hidden_reasoning_omitted": True}
        binary = payload["events"][1]["content"]["parts"][0]["function_response"]["response"][
            "inline_image"
        ]
        assert binary["binary_omitted"] is True
        assert binary["size"] == len(b"private-image-bytes")
        assert payload["usage"]["thinking_tokens"] == 5
        assert payload["response"] == {"best_guess": "Air-fried steak"}
        for forbidden in (
            "request-secret",
            "tool-secret",
            "hidden internal reasoning",
            "hidden response reasoning",
            "private-image-bytes",
        ):
            assert forbidden not in serialized

        with pytest.raises(AiTraceNotFound):
            await service.read(account_id=foreign.id, trace_id=record.id)

    asyncio.run(scenario())


def test_trace_audit_rejects_hidden_reasoning_and_credentials() -> None:
    base = {
        "schema_version": "application-visible-ai-trace-v1",
        "trace_id": "trace-test",
        "account_id": "account-test",
        "event_id": "event-test",
        "lineage": {},
        "versions": {},
        "request": {
            "model": "gemini-test",
            "system_instruction": "Classify the event.",
            "user_content": {},
            "response_schema": {},
            "tools": [],
            "run_config": {},
        },
        "events": [
            {"function_call": {}},
            {"function_response": {}},
        ],
        "response": {},
        "validation_failures": [],
        "error": None,
        "usage": {},
        "timing": {},
    }
    with pytest.raises(AiTraceIntegrityError, match="hidden reasoning"):
        audit_application_visible_trace({**base, "reasoning": "private"})
    with pytest.raises(AiTraceIntegrityError, match="credential field"):
        audit_application_visible_trace({**base, "access_token": "private"})
    with pytest.raises(AiTraceIntegrityError, match="credential-shaped string"):
        audit_application_visible_trace({**base, "response": {"text": "Bearer super-secret-token"}})


def test_trace_integrity_and_repair_lineage_fail_closed() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("trace-integrity-owner")
        store = InMemoryObjectStore()
        service = AiTraceService(repository=repository, object_store=store)
        root_spec = _spec(account_id=account.id, invocation_key="bounded-repair")
        repair_spec = _spec(
            account_id=account.id,
            invocation_key="bounded-repair:repair:1",
        )
        capture = AiTraceCapture(spec=repair_spec, request={"repair": "invalid output"})
        failure = ValueError("missing required best_guess; Bearer failure-secret")
        record = await service.persist(
            capture=capture,
            usage=_usage(repair_spec, succeeded=False),
            error=failure,
        )
        payload = await service.read(account_id=account.id, trace_id=record.id)
        assert (
            record.root_trace_id
            == AiTraceCapture(
                spec=root_spec,
                request={},
            ).trace_id
        )
        assert record.parent_trace_id == record.root_trace_id
        assert payload["validation_failures"] == ["missing required best_guess; [REDACTED]"]
        assert payload["error"]["message"] == ("missing required best_guess; [REDACTED]")

        content, content_type = store._objects[record.object_key]
        store._objects[record.object_key] = (content + b"tampered", content_type)
        with pytest.raises(AiTraceIntegrityError, match="hash"):
            await service.read(account_id=account.id, trace_id=record.id)

    asyncio.run(scenario())
