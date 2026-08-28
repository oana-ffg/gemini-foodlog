from __future__ import annotations

import os

from .domain import MailQuotaPolicy

DEFAULT_MAX_RETAINED_MESSAGES = 400
DEFAULT_MAX_RETAINED_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_RATE_MESSAGES = 30
DEFAULT_MAX_RATE_BYTES = 64 * 1024 * 1024
DEFAULT_RATE_WINDOW_SECONDS = 3_600


def _positive_environment_integer(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def quota_policy_from_environment() -> MailQuotaPolicy:
    return MailQuotaPolicy(
        max_retained_messages=_positive_environment_integer(
            "FOODLOG_MAIL_MAX_RETAINED_MESSAGES",
            DEFAULT_MAX_RETAINED_MESSAGES,
        ),
        max_retained_bytes=_positive_environment_integer(
            "FOODLOG_MAIL_MAX_RETAINED_BYTES",
            DEFAULT_MAX_RETAINED_BYTES,
        ),
        max_rate_messages=_positive_environment_integer(
            "FOODLOG_MAIL_MAX_RATE_MESSAGES",
            DEFAULT_MAX_RATE_MESSAGES,
        ),
        max_rate_bytes=_positive_environment_integer(
            "FOODLOG_MAIL_MAX_RATE_BYTES",
            DEFAULT_MAX_RATE_BYTES,
        ),
        rate_window_seconds=_positive_environment_integer(
            "FOODLOG_MAIL_RATE_WINDOW_SECONDS",
            DEFAULT_RATE_WINDOW_SECONDS,
        ),
    )
