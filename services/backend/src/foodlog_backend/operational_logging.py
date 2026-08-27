from __future__ import annotations

import json
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, Response

TRACE_CONTEXT_PATTERN = re.compile(r"^(?P<trace>[0-9a-f]{32})(?:/[0-9]+)?(?:;o=[01])?$")
SAFE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._:/{}-]{1,240}$")
SAFE_FIELDS = frozenset(
    {
        "accepted_image_count",
        "account_id",
        "account_capacity_count",
        "account_capacity_limit",
        "actual_dkk_micros",
        "capture_id",
        "delivery_attempt",
        "duration_ms",
        "environment",
        "error_kind",
        "event_id",
        "http_method",
        "http_route",
        "http_status",
        "job_id",
        "mail_id",
        "message_id",
        "model",
        "notification_id",
        "outcome",
        "purpose",
        "request_id",
        "retry_attempt",
        "service",
        "subject_revision",
        "total_tokens",
        "trace_id",
        "trial_image_limit",
        "workload",
    }
)


@dataclass(frozen=True)
class RequestLogContext:
    request_id: str
    trace_id: str | None


REQUEST_LOG_CONTEXT: ContextVar[RequestLogContext | None] = ContextVar(
    "foodlog_request_log_context",
    default=None,
)


def _validated_fields(fields: dict[str, str | int | None]) -> dict[str, str | int]:
    unknown = set(fields) - SAFE_FIELDS
    if unknown:
        raise ValueError(f"unsupported operational log fields: {sorted(unknown)}")
    result: dict[str, str | int] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise TypeError(f"operational log field {key} has an unsafe type")
        if isinstance(value, str) and not SAFE_VALUE_PATTERN.fullmatch(value):
            value = f"sha256:{sha256(value.encode()).hexdigest()}"
        result[key] = value
    return result


def emit_operational_event(
    severity: str,
    event: str,
    **fields: str | int | None,
) -> None:
    if severity not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("invalid operational event severity")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,79}", event):
        raise ValueError("operational event names must be stable lowercase identifiers")
    context = REQUEST_LOG_CONTEXT.get()
    if context is not None:
        fields.setdefault("request_id", context.request_id)
        fields.setdefault("trace_id", context.trace_id)
    payload: dict[str, str | int] = {
        "schema": "foodlog_operational_event_v1",
        "severity": severity,
        "event": event,
        **_validated_fields(fields),
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


def safe_error_kind(error: BaseException) -> str:
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,119}", name) else "Exception"


def trace_id_from_header(value: str | None) -> str | None:
    if value is None:
        return None
    match = TRACE_CONTEXT_PATTERN.fullmatch(value)
    return match.group("trace") if match else None


def _set_request_context(request: Request) -> Token[RequestLogContext | None]:
    return REQUEST_LOG_CONTEXT.set(
        RequestLogContext(
            request_id=uuid4().hex,
            trace_id=trace_id_from_header(request.headers.get("x-cloud-trace-context")),
        )
    )


def install_request_logging(
    app: FastAPI,
    *,
    service: str,
    environment: str,
) -> None:
    @app.middleware("http")
    async def operational_request_log(request: Request, call_next) -> Response:
        token = _set_request_context(request)
        started = perf_counter()
        response: Response | None = None
        error: BaseException | None = None
        try:
            response = await call_next(request)
            return response
        except BaseException as caught:
            error = caught
            raise
        finally:
            route = request.scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            emit_operational_event(
                "ERROR" if error is not None else "INFO",
                "http_request_completed" if error is None else "http_request_failed",
                service=service,
                environment=environment,
                http_method=request.method,
                http_route=route_template,
                http_status=response.status_code if response is not None else 500,
                duration_ms=max(0, round((perf_counter() - started) * 1_000)),
                error_kind=safe_error_kind(error) if error is not None else None,
            )
            if response is not None:
                context = REQUEST_LOG_CONTEXT.get()
                assert context is not None
                response.headers["X-Request-ID"] = context.request_id
            REQUEST_LOG_CONTEXT.reset(token)
