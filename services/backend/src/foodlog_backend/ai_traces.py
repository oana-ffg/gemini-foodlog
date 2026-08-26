from __future__ import annotations

import gzip
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any, Protocol

from pydantic import BaseModel

from .model_accounting import ModelInvocationSpec
from .models import AiTraceRecord, ModelUsageRecord, utc_now
from .storage import GCSObjectStore, ObjectStore

TRACE_SCHEMA_VERSION = "application-visible-ai-trace-v1"
MAX_TRACE_EVENTS = 100
MAX_TRACE_JSON_BYTES = 20_000_000
MAX_TRACE_COMPRESSED_BYTES = 10_000_000
MAX_TRACE_STRING_CHARS = 100_000

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "setcookie",
    "password",
    "secret",
    "apikey",
    "accesskey",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "bearertoken",
    "cameracredential",
    "devicecredential",
    "rawheaders",
    "headers",
    "requestedauthconfigs",
}
_HIDDEN_REASONING_KEYS = {
    "thought",
    "thoughts",
    "thoughtsignature",
    "chainofthought",
    "reasoning",
    "reasoningcontent",
}
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/]+=*")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_GOOGLE_API_KEY_PATTERN = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")


class AiTraceRepository(Protocol):
    async def record_ai_trace(self, trace: AiTraceRecord) -> AiTraceRecord: ...

    async def ai_trace_for_account(
        self,
        *,
        account_id: str,
        trace_id: str,
    ) -> AiTraceRecord: ...


class AiTraceIntegrityError(RuntimeError):
    pass


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _redact_string(value: str) -> str:
    bounded = value[:MAX_TRACE_STRING_CHARS]
    bounded = _BEARER_PATTERN.sub(_REDACTED, bounded)
    bounded = _JWT_PATTERN.sub(_REDACTED, bounded)
    return _GOOGLE_API_KEY_PATTERN.sub(_REDACTED, bounded)


def redact_application_visible(value: Any) -> Any:
    """Return JSON-safe application evidence with secrets and hidden thoughts removed."""
    if isinstance(value, BaseModel):
        return redact_application_visible(value.model_dump(mode="python", exclude_none=True))
    if isinstance(value, Mapping):
        normalized = {_normalized_key(key): item for key, item in value.items()}
        if normalized.get("thought") is True:
            return {"hidden_reasoning_omitted": True}
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized_key = _normalized_key(key)
            if normalized_key in _HIDDEN_REASONING_KEYS:
                continue
            if normalized_key in _SENSITIVE_KEYS or (
                normalized_key.endswith("token") and isinstance(item, (str, bytes))
            ):
                redacted[key] = _REDACTED
            else:
                redacted[key] = redact_application_visible(item)
        return redacted
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_application_visible(item) for item in value]
    if isinstance(value, bytes):
        return {
            "binary_omitted": True,
            "size": len(value),
            "sha256": sha256(value).hexdigest(),
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value))


def trace_id_for_invocation(
    *,
    account_id: str,
    event_id: str,
    invocation_key: str,
) -> str:
    identity = f"application-visible-ai-trace-v1\0{account_id}\0{event_id}\0{invocation_key}"
    return f"trace-{sha256(identity.encode()).hexdigest()}"


class AiTraceCapture:
    def __init__(
        self,
        *,
        spec: ModelInvocationSpec,
        request: Mapping[str, Any],
        started_at: datetime | None = None,
    ) -> None:
        self.spec = spec
        self.started_at = started_at or utc_now()
        self.request = redact_application_visible(request)
        self.events: list[dict[str, Any]] = []

    @property
    def trace_id(self) -> str:
        return trace_id_for_invocation(
            account_id=self.spec.account_id,
            event_id=self.spec.event_id,
            invocation_key=self.spec.invocation_key,
        )

    @property
    def root_trace_id(self) -> str:
        root_key = self.spec.invocation_key.split(":repair:", 1)[0]
        return trace_id_for_invocation(
            account_id=self.spec.account_id,
            event_id=self.spec.event_id,
            invocation_key=root_key,
        )

    def record_event(self, event: Any) -> None:
        if len(self.events) >= MAX_TRACE_EVENTS:
            raise ValueError("ADK trace exceeded its bounded event count")
        visible = redact_application_visible(event)
        if not isinstance(visible, dict):
            raise TypeError("ADK event trace must serialize to an object")
        self.events.append(visible)

    def payload(
        self,
        *,
        usage: ModelUsageRecord,
        response: Mapping[str, Any] | None,
        error: BaseException | None,
        completed_at: datetime,
    ) -> dict[str, Any]:
        error_code = usage.error_code
        validation_failures = []
        if error_code == "InvalidModelOutputError" and error is not None:
            validation_failures.append(_redact_string(str(error))[:1_200])
        error_payload = None
        if error is not None:
            error_payload = {
                "code": error_code or type(error).__name__,
                "message": _redact_string(str(error))[:2_000],
            }
        return redact_application_visible(
            {
                "schema_version": TRACE_SCHEMA_VERSION,
                "trace_id": self.trace_id,
                "root_trace_id": self.root_trace_id,
                "parent_trace_id": (
                    self.root_trace_id if self.trace_id != self.root_trace_id else None
                ),
                "account_id": self.spec.account_id,
                "event_id": self.spec.event_id,
                "invocation_key_sha256": sha256(self.spec.invocation_key.encode()).hexdigest(),
                "lineage": {
                    "purpose": self.spec.purpose,
                    "retry_attempt": self.spec.retry_attempt,
                    "evaluation": self.spec.evaluation,
                    "reservation_id": usage.reservation_id,
                    "provider_invocation_id": usage.invocation_id,
                },
                "versions": {
                    "model": usage.model,
                    "model_version": usage.model_version,
                    "region": usage.region,
                    "prompt_version": usage.prompt_version,
                },
                "request": self.request,
                "events": self.events,
                "response": response,
                "validation_failures": validation_failures,
                "error": error_payload,
                "usage": {
                    "outcome": usage.outcome,
                    "prompt_tokens": usage.prompt_tokens,
                    "response_tokens": usage.response_tokens,
                    "thinking_tokens": usage.thinking_tokens,
                    "total_tokens": usage.total_tokens,
                    "actual_usd_nanos": usage.actual_usd_nanos,
                    "actual_dkk_micros": usage.actual_dkk_micros,
                    "reserved_dkk_micros": usage.reserved_dkk_micros,
                },
                "timing": {
                    "started_at": self.started_at,
                    "completed_at": completed_at,
                    "latency_ms": max(
                        0,
                        int((completed_at - self.started_at).total_seconds() * 1_000),
                    ),
                },
            }
        )


class AiTraceService:
    def __init__(
        self,
        *,
        repository: AiTraceRepository,
        object_store: ObjectStore,
    ) -> None:
        self._repository = repository
        self._object_store = object_store

    async def persist(
        self,
        *,
        capture: AiTraceCapture,
        usage: ModelUsageRecord,
        response: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
        completed_at: datetime | None = None,
    ) -> AiTraceRecord:
        finished_at = completed_at or utc_now()
        payload = capture.payload(
            usage=usage,
            response=response,
            error=error,
            completed_at=finished_at,
        )
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        if len(canonical) > MAX_TRACE_JSON_BYTES:
            raise ValueError("AI trace exceeds its uncompressed size bound")
        compressed = gzip.compress(canonical, compresslevel=9, mtime=0)
        if len(compressed) > MAX_TRACE_COMPRESSED_BYTES:
            raise ValueError("AI trace exceeds its compressed size bound")
        object_key = f"accounts/{capture.spec.account_id}/traces/{capture.trace_id}.json.gz"
        digest = sha256(compressed).hexdigest()
        await self._object_store.put(
            capture.spec.account_id,
            object_key,
            compressed,
            "application/gzip",
        )
        trace = AiTraceRecord(
            id=capture.trace_id,
            account_id=capture.spec.account_id,
            event_id=capture.spec.event_id,
            reservation_id=usage.reservation_id,
            root_trace_id=capture.root_trace_id,
            parent_trace_id=(
                capture.root_trace_id if capture.trace_id != capture.root_trace_id else None
            ),
            object_key=object_key,
            content_sha256=digest,
            compressed_size=len(compressed),
            status=usage.outcome,
            model=usage.model,
            model_version=usage.model_version,
            provider_invocation_id=usage.invocation_id,
            region=usage.region,
            prompt_version=usage.prompt_version,
            purpose=usage.purpose,
            retry_attempt=usage.retry_attempt,
            evaluation=usage.evaluation,
            prompt_tokens=usage.prompt_tokens,
            response_tokens=usage.response_tokens,
            thinking_tokens=usage.thinking_tokens,
            total_tokens=usage.total_tokens,
            actual_dkk_micros=usage.actual_dkk_micros,
            latency_ms=payload["timing"]["latency_ms"],
            error_code=usage.error_code,
            started_at=capture.started_at,
            completed_at=finished_at,
            created_at=finished_at,
        )
        return await self._repository.record_ai_trace(trace)

    async def read(self, *, account_id: str, trace_id: str) -> dict[str, Any]:
        trace = await self._repository.ai_trace_for_account(
            account_id=account_id,
            trace_id=trace_id,
        )
        compressed = await self._object_store.get(account_id, trace.object_key)
        if sha256(compressed).hexdigest() != trace.content_sha256:
            raise AiTraceIntegrityError("AI trace object hash does not match its index")
        try:
            payload = json.loads(gzip.decompress(compressed))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AiTraceIntegrityError("AI trace object is not valid compressed JSON") from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != TRACE_SCHEMA_VERSION
            or payload.get("trace_id") != trace.id
            or payload.get("account_id") != account_id
        ):
            raise AiTraceIntegrityError("AI trace payload identity does not match its index")
        return payload


def trace_service_from_environment(
    repository: AiTraceRepository,
) -> AiTraceService | None:
    bucket_name = os.environ.get("FOODLOG_TRACE_BUCKET")
    if not bucket_name:
        return None
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required when trace storage is enabled")
    return AiTraceService(
        repository=repository,
        object_store=GCSObjectStore(project_id=project_id, bucket_name=bucket_name),
    )
