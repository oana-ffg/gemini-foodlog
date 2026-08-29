import asyncio
import gzip
import json
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest

from foodlog_backend.ai_traces import AiTraceService
from foodlog_backend.models import (
    ActivityEvent,
    AiTraceRecord,
    AuditPurpose,
    CaptureRecord,
    CaptureStatus,
    DurableJob,
    JobKind,
    JobStatus,
    utc_now,
)
from foodlog_backend.operator_diagnostics import (
    CloudLoggingDiagnosticReader,
    DiagnosticLogEntry,
    OperatorDiagnosticService,
)
from foodlog_backend.storage import InMemoryObjectStore


class DiagnosticRepositoryStub:
    def __init__(
        self,
        *,
        event: ActivityEvent,
        captures: list[CaptureRecord],
        jobs: list[DurableJob],
        traces: list[AiTraceRecord],
    ) -> None:
        self.event = event
        self.captures = captures
        self.jobs = {job.id: job for job in jobs}
        self.traces = {trace.id: trace for trace in traces}
        self.audit_events = []
        self.calls = []

    async def append_audit_event(self, event):
        self.calls.append("audit")
        self.audit_events.append(event)
        return event

    async def event_evidence_for_account(self, *, account_id: str, event_id: str):
        self.calls.append("event")
        if account_id != self.event.account_id or event_id != self.event.id:
            raise LookupError("event not found")
        return self.event, self.captures

    async def job_for_account(self, account_id: str, job_id: str):
        assert account_id == self.event.account_id
        return self.jobs.get(job_id)

    async def ai_traces_for_event(self, *, account_id: str, event_id: str, limit: int = 25):
        assert account_id == self.event.account_id
        assert event_id == self.event.id
        assert limit == 25
        return list(self.traces.values())

    async def ai_trace_for_account(self, *, account_id: str, trace_id: str):
        trace = self.traces[trace_id]
        assert trace.account_id == account_id
        return trace


class LogReaderStub:
    async def read_event_logs(self, *, account_id: str, event_id: str, limit: int = 100):
        return [
            DiagnosticLogEntry(
                timestamp=utc_now(),
                severity="INFO",
                event="event_inference_completed",
                log_name="run.googleapis.com%2Fstdout",
                resource_type="cloud_run_revision",
                fields={"account_id": account_id, "event_id": event_id, "total_tokens": 42},
            )
        ]


def test_operator_diagnostic_is_audited_first_and_emits_metadata_only() -> None:
    now = utc_now()
    account_id = "account-a"
    event_id = "event-a"
    capture_id = "capture-a"
    trace_id = f"trace-{'a' * 64}"
    event = ActivityEvent(
        id=event_id,
        account_id=account_id,
        camera_ids=["camera-a"],
        first_capture_at=now,
        last_capture_at=now,
        capture_count=1,
        grouping_policy_version="grouping-v1",
    )
    capture_bytes = b"private-image-bytes-never-returned"
    capture = CaptureRecord(
        id=capture_id,
        account_id=account_id,
        camera_id="camera-a",
        idempotency_key="idempotency-a",
        content_type="image/jpeg",
        content_sha256=sha256(capture_bytes).hexdigest(),
        object_key=f"accounts/{account_id}/captures/{capture_id}.jpg",
        status=CaptureStatus.STORED,
        event_id=event_id,
    )
    job = DurableJob(
        id=f"event-inference-{event_id}",
        account_id=account_id,
        kind=JobKind.EVENT_INFERENCE,
        subject_id=event_id,
        subject_revision=1,
        status=JobStatus.COMPLETED,
        completed_at=now,
    )
    private_trace_text = "PRIVATE TRACE PROMPT AND RESPONSE"
    payload = {
        "schema_version": "application-visible-ai-trace-v1",
        "trace_id": trace_id,
        "account_id": account_id,
        "event_id": event_id,
        "lineage": {},
        "versions": {},
        "request": {
            "model": "gemini",
            "system_instruction": private_trace_text,
            "user_content": private_trace_text,
            "response_schema": {},
            "tools": [],
            "run_config": {},
        },
        "events": [
            {
                "functionCall": {"name": "context"},
                "functionResponse": {"name": "context", "response": private_trace_text},
            }
        ],
        "response": {"text": private_trace_text},
        "validation_failures": [],
        "error": None,
        "usage": {"outcome": "succeeded"},
        "timing": {},
    }
    compressed = gzip.compress(json.dumps(payload, sort_keys=True).encode(), mtime=0)
    trace = AiTraceRecord(
        id=trace_id,
        account_id=account_id,
        event_id=event_id,
        reservation_id=f"model-{'b' * 64}",
        root_trace_id=trace_id,
        object_key=f"accounts/{account_id}/traces/{trace_id}.json.gz",
        content_sha256=sha256(compressed).hexdigest(),
        compressed_size=len(compressed),
        status="succeeded",
        model="gemini-3.6-flash",
        model_version="gemini-3.6-flash-001",
        region="eu",
        prompt_version="food-event-v1",
        purpose="event_inference",
        retry_attempt=0,
        evaluation=False,
        prompt_tokens=30,
        response_tokens=12,
        thinking_tokens=0,
        total_tokens=42,
        actual_dkk_micros=100,
        latency_ms=500,
        started_at=now - timedelta(milliseconds=500),
        completed_at=now,
        created_at=now,
    )
    repository = DiagnosticRepositoryStub(
        event=event,
        captures=[capture],
        jobs=[job],
        traces=[trace],
    )
    media_store = InMemoryObjectStore()
    trace_store = InMemoryObjectStore()
    asyncio.run(media_store.put(account_id, capture.object_key, capture_bytes, "image/jpeg"))
    asyncio.run(trace_store.put(account_id, trace.object_key, compressed, "application/gzip"))
    service = OperatorDiagnosticService(
        repository=repository,
        media_store=media_store,
        trace_service=AiTraceService(repository=repository, object_store=trace_store),
        log_reader=LogReaderStub(),
    )

    result = asyncio.run(
        service.inspect_event(
            account_id=account_id,
            event_id=event_id,
            purpose=AuditPurpose.DEVELOPMENT_VERIFICATION,
            session_id="diagnostic-session-a",
        )
    )
    serialized = result.model_dump_json()

    assert repository.calls[:2] == ["audit", "event"]
    assert repository.audit_events[0].purpose == AuditPurpose.DEVELOPMENT_VERIFICATION
    assert result.event.id == event_id
    assert result.captures[0].object.size == len(capture_bytes)
    assert result.traces[0].integrity["redaction_verified"] is True
    assert private_trace_text not in serialized
    assert capture_bytes.decode() not in serialized
    assert "system_instruction" not in serialized
    assert '"response":' not in serialized


def test_operator_diagnostic_records_attempt_before_missing_event_failure() -> None:
    now = utc_now()
    repository = DiagnosticRepositoryStub(
        event=ActivityEvent(
            id="event-a",
            account_id="account-a",
            camera_ids=["camera-a"],
            first_capture_at=now,
            last_capture_at=now,
            capture_count=1,
            grouping_policy_version="grouping-v1",
        ),
        captures=[],
        jobs=[],
        traces=[],
    )
    service = OperatorDiagnosticService(
        repository=repository,
        media_store=InMemoryObjectStore(),
        trace_service=AiTraceService(
            repository=repository,
            object_store=InMemoryObjectStore(),
        ),
        log_reader=LogReaderStub(),
    )

    with pytest.raises(LookupError):
        asyncio.run(
            service.inspect_event(
                account_id="account-a",
                event_id="event-missing",
                purpose=AuditPurpose.INCIDENT_TRIAGE,
                session_id="diagnostic-session-b",
            )
        )

    assert repository.calls == ["audit", "event"]
    assert repository.audit_events[0].subject_id == "event-missing"


def test_cloud_log_reader_fails_closed_on_unknown_payload_fields() -> None:
    entry = SimpleNamespace(
        payload={
            "schema": "foodlog_operational_event_v1",
            "severity": "INFO",
            "event": "event_processed",
            "account_id": "account-a",
            "event_id": "event-a",
            "private_payload": "must not escape",
        },
        timestamp=utc_now(),
        severity="INFO",
        log_name="projects/project-a/logs/stdout",
        resource=SimpleNamespace(type="cloud_run_revision"),
    )

    with pytest.raises(ValueError, match="unsupported fields"):
        CloudLoggingDiagnosticReader._safe_entry(
            entry,
            account_id="account-a",
            event_id="event-a",
        )


def test_cloud_log_reader_normalizes_integral_protobuf_numbers_only() -> None:
    entry = SimpleNamespace(
        payload={
            "schema": "foodlog_operational_event_v1",
            "severity": "INFO",
            "event": "event_processed",
            "account_id": "account-a",
            "event_id": "event-a",
            "delivery_attempt": 2.0,
        },
        timestamp=utc_now(),
        severity="INFO",
        log_name="projects/project-a/logs/stdout",
        resource=SimpleNamespace(type="cloud_run_revision"),
    )

    safe = CloudLoggingDiagnosticReader._safe_entry(
        entry,
        account_id="account-a",
        event_id="event-a",
    )
    assert safe.fields["delivery_attempt"] == 2

    entry.payload["delivery_attempt"] = 2.5
    with pytest.raises(ValueError, match="unsafe type"):
        CloudLoggingDiagnosticReader._safe_entry(
            entry,
            account_id="account-a",
            event_id="event-a",
        )
