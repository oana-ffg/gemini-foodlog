from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from google.cloud import logging_v2
from pydantic import BaseModel, ConfigDict, Field

from .ai_traces import AiTraceService, audit_application_visible_trace
from .audit import build_audit_event
from .models import (
    ActivityEvent,
    AiTraceRecord,
    AuditAction,
    AuditActorKind,
    AuditEvent,
    AuditPurpose,
    AuditSource,
    CaptureRecord,
    DurableJob,
    capture_grouping_job_id,
    event_inference_job_id,
)
from .operational_logging import SAFE_FIELDS
from .storage import ObjectMetadata, ObjectStore

MAX_DIAGNOSTIC_CAPTURES = 200
MAX_DIAGNOSTIC_TRACES = 25
MAX_DIAGNOSTIC_LOG_ENTRIES = 100
OPERATIONAL_LOG_SCHEMA = "foodlog_operational_event_v1"


class DiagnosticRepository(Protocol):
    async def append_audit_event(self, event: AuditEvent) -> AuditEvent: ...

    async def event_evidence_for_account(
        self,
        *,
        account_id: str,
        event_id: str,
    ) -> tuple[ActivityEvent, list[CaptureRecord]]: ...

    async def job_for_account(self, account_id: str, job_id: str) -> DurableJob | None: ...

    async def ai_traces_for_event(
        self,
        *,
        account_id: str,
        event_id: str,
        limit: int = MAX_DIAGNOSTIC_TRACES,
    ) -> list[AiTraceRecord]: ...


class DiagnosticLogReader(Protocol):
    async def read_event_logs(
        self,
        *,
        account_id: str,
        event_id: str,
        limit: int = MAX_DIAGNOSTIC_LOG_ENTRIES,
    ) -> list[DiagnosticLogEntry]: ...


class DiagnosticModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosticObjectMetadata(DiagnosticModel):
    key: str
    size: int = Field(ge=0)
    content_type: str | None
    generation: int | None
    crc32c: str | None
    updated_at: datetime | None

    @classmethod
    def from_object(cls, metadata: ObjectMetadata) -> DiagnosticObjectMetadata:
        return cls(**metadata.__dict__)


class DiagnosticJob(DiagnosticModel):
    id: str
    kind: str
    status: str
    attempt_count: int
    last_error_code: str | None

    @classmethod
    def from_job(cls, job: DurableJob) -> DiagnosticJob:
        return cls(
            id=job.id,
            kind=job.kind.value,
            status=job.status.value,
            attempt_count=job.attempt_count,
            last_error_code=job.last_error_code,
        )


class DiagnosticCapture(DiagnosticModel):
    id: str
    camera_id: str
    status: str
    created_at: datetime
    content_type: str
    content_sha256: str
    object: DiagnosticObjectMetadata
    grouping_job: DiagnosticJob | None


class DiagnosticEvent(DiagnosticModel):
    id: str
    status: str
    current_revision: int
    camera_ids: list[str]
    capture_count: int
    meal_id: str | None
    first_capture_at: datetime
    last_capture_at: datetime
    inference_job: DiagnosticJob | None


class DiagnosticTrace(DiagnosticModel):
    id: str
    status: str
    model: str
    model_version: str | None
    prompt_version: str | None
    purpose: str
    retry_attempt: int
    total_tokens: int
    actual_dkk_micros: int
    latency_ms: int
    error_code: str | None
    created_at: datetime
    integrity: dict[str, int | bool]


class DiagnosticLogEntry(DiagnosticModel):
    timestamp: datetime | None
    severity: str | None
    event: str
    log_name: str | None
    resource_type: str | None
    fields: dict[str, str | int]


class OperatorDiagnosticResult(DiagnosticModel):
    schema_version: str = "foodlog-operator-diagnostic-v1"
    audit_event_id: str
    account_id: str
    purpose: AuditPurpose
    event: DiagnosticEvent
    captures: list[DiagnosticCapture]
    traces: list[DiagnosticTrace]
    logs: list[DiagnosticLogEntry]


class CloudLoggingDiagnosticReader:
    """Read only bounded, structured operational records for one exact tenant event."""

    def __init__(
        self,
        *,
        project_id: str,
        client: logging_v2.Client | None = None,
    ) -> None:
        self._project_id = project_id
        self._client = client or logging_v2.Client(project=project_id)

    def _read_entries(self, *, account_id: str, event_id: str, limit: int) -> list[Any]:
        filter_ = (
            f'jsonPayload.schema="{OPERATIONAL_LOG_SCHEMA}" '
            f'AND jsonPayload.account_id="{account_id}" '
            f'AND jsonPayload.event_id="{event_id}"'
        )
        return list(
            self._client.list_entries(
                resource_names=[f"projects/{self._project_id}"],
                filter_=filter_,
                order_by=logging_v2.DESCENDING,
                max_results=limit,
                page_size=limit,
            )
        )

    async def read_event_logs(
        self,
        *,
        account_id: str,
        event_id: str,
        limit: int = MAX_DIAGNOSTIC_LOG_ENTRIES,
    ) -> list[DiagnosticLogEntry]:
        if not 1 <= limit <= MAX_DIAGNOSTIC_LOG_ENTRIES:
            raise ValueError("diagnostic log limit must be between 1 and 100")
        entries = await asyncio.to_thread(
            self._read_entries,
            account_id=account_id,
            event_id=event_id,
            limit=limit,
        )
        return [
            self._safe_entry(entry, account_id=account_id, event_id=event_id)
            for entry in entries
        ]

    @staticmethod
    def _safe_entry(entry: Any, *, account_id: str, event_id: str) -> DiagnosticLogEntry:
        payload = entry.payload
        if not isinstance(payload, Mapping):
            raise ValueError("diagnostic log entry is not structured JSON")
        if (
            payload.get("schema") != OPERATIONAL_LOG_SCHEMA
            or payload.get("account_id") != account_id
            or payload.get("event_id") != event_id
        ):
            raise ValueError("diagnostic log entry escaped its requested scope")
        structural_fields = {"schema", "severity", "event"}
        unknown = set(payload) - structural_fields - SAFE_FIELDS
        if unknown:
            raise ValueError(f"diagnostic log contains unsupported fields: {sorted(unknown)}")
        event = payload.get("event")
        if not isinstance(event, str):
            raise ValueError("diagnostic log event name is invalid")
        fields: dict[str, str | int] = {}
        for key in sorted(set(payload) & SAFE_FIELDS):
            value = payload[key]
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise ValueError(f"diagnostic log field {key} has an unsafe type")
            fields[key] = value
        resource = getattr(entry, "resource", None)
        resource_type = getattr(resource, "type", None)
        log_name = getattr(entry, "log_name", None)
        return DiagnosticLogEntry(
            timestamp=getattr(entry, "timestamp", None),
            severity=str(getattr(entry, "severity", "")) or None,
            event=event,
            log_name=log_name.rsplit("/", 1)[-1] if isinstance(log_name, str) else None,
            resource_type=resource_type if isinstance(resource_type, str) else None,
            fields=fields,
        )


class OperatorDiagnosticService:
    def __init__(
        self,
        *,
        repository: DiagnosticRepository,
        media_store: ObjectStore,
        trace_service: AiTraceService,
        log_reader: DiagnosticLogReader,
    ) -> None:
        self._repository = repository
        self._media_store = media_store
        self._trace_service = trace_service
        self._log_reader = log_reader

    async def inspect_event(
        self,
        *,
        account_id: str,
        event_id: str,
        purpose: AuditPurpose,
        session_id: str,
    ) -> OperatorDiagnosticResult:
        audit = await self._repository.append_audit_event(
            build_audit_event(
                account_id=account_id,
                action=AuditAction.OPERATOR_DIAGNOSTIC_READ,
                actor_kind=AuditActorKind.OPERATOR,
                source=AuditSource.OPERATOR_CLI,
                subject_kind="activity_event",
                subject_id=event_id,
                purpose=purpose,
                occurrence_id=session_id,
            )
        )
        event, captures = await self._repository.event_evidence_for_account(
            account_id=account_id,
            event_id=event_id,
        )
        if len(captures) > MAX_DIAGNOSTIC_CAPTURES:
            raise ValueError("event evidence exceeds the 200-capture diagnostic bound")

        async def capture_diagnostic(capture: CaptureRecord) -> DiagnosticCapture:
            metadata, job = await asyncio.gather(
                self._media_store.metadata(account_id, capture.object_key),
                self._repository.job_for_account(
                    account_id,
                    capture_grouping_job_id(capture.id),
                ),
            )
            if metadata.content_type != capture.content_type:
                raise ValueError("capture object content type disagrees with Firestore evidence")
            return DiagnosticCapture(
                id=capture.id,
                camera_id=capture.camera_id,
                status=capture.status.value,
                created_at=capture.created_at,
                content_type=capture.content_type,
                content_sha256=capture.content_sha256,
                object=DiagnosticObjectMetadata.from_object(metadata),
                grouping_job=DiagnosticJob.from_job(job) if job is not None else None,
            )

        trace_records, inference_job, logs, capture_results = await asyncio.gather(
            self._repository.ai_traces_for_event(
                account_id=account_id,
                event_id=event_id,
                limit=MAX_DIAGNOSTIC_TRACES,
            ),
            self._repository.job_for_account(account_id, event_inference_job_id(event_id)),
            self._log_reader.read_event_logs(account_id=account_id, event_id=event_id),
            asyncio.gather(*(capture_diagnostic(capture) for capture in captures)),
        )

        trace_results: list[DiagnosticTrace] = []
        for trace in trace_records:
            payload = await self._trace_service.read(account_id=account_id, trace_id=trace.id)
            if payload.get("event_id") != event_id:
                raise ValueError("AI trace payload escaped the requested event scope")
            trace_results.append(
                DiagnosticTrace(
                    id=trace.id,
                    status=trace.status,
                    model=trace.model,
                    model_version=trace.model_version,
                    prompt_version=trace.prompt_version,
                    purpose=trace.purpose,
                    retry_attempt=trace.retry_attempt,
                    total_tokens=trace.total_tokens,
                    actual_dkk_micros=trace.actual_dkk_micros,
                    latency_ms=trace.latency_ms,
                    error_code=trace.error_code,
                    created_at=trace.created_at,
                    integrity=audit_application_visible_trace(payload),
                )
            )

        return OperatorDiagnosticResult(
            audit_event_id=audit.id,
            account_id=account_id,
            purpose=purpose,
            event=DiagnosticEvent(
                id=event.id,
                status=event.status.value,
                current_revision=event.current_revision,
                camera_ids=event.camera_ids,
                capture_count=event.capture_count,
                meal_id=event.meal_id,
                first_capture_at=event.first_capture_at,
                last_capture_at=event.last_capture_at,
                inference_job=(
                    DiagnosticJob.from_job(inference_job) if inference_job is not None else None
                ),
            ),
            captures=list(capture_results),
            traces=trace_results,
            logs=logs,
        )
