from __future__ import annotations

import json
import re
from hashlib import sha256

SAFE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._:/{}-]{1,240}$")
SAFE_FIELDS = frozenset(
    {
        "account_id",
        "duration_ms",
        "environment",
        "error_kind",
        "http_method",
        "http_route",
        "http_status",
        "mail_id",
        "outcome",
        "request_id",
        "service",
        "trace_id",
    }
)


def emit_gateway_event(
    severity: str,
    event: str,
    **fields: str | int | None,
) -> None:
    if severity not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("invalid operational event severity")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,79}", event):
        raise ValueError("invalid operational event name")
    payload: dict[str, str | int] = {
        "schema": "foodlog_operational_event_v1",
        "severity": severity,
        "event": event,
    }
    for key, value in fields.items():
        if key not in SAFE_FIELDS:
            raise ValueError(f"unsupported operational log field: {key}")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise TypeError(f"unsafe operational log field type: {key}")
        if isinstance(value, str) and not SAFE_VALUE_PATTERN.fullmatch(value):
            value = f"sha256:{sha256(value.encode()).hexdigest()}"
        payload[key] = value
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), flush=True)


def safe_error_kind(error: BaseException) -> str:
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,119}", name) else "Exception"


def trace_id_from_header(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"(?P<trace>[0-9a-f]{32})(?:/[0-9]+)?(?:;o=[01])?", value)
    return match.group("trace") if match else None
