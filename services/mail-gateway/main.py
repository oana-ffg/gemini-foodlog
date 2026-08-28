from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from time import perf_counter
from urllib.parse import unquote
from uuid import uuid4

from mail_gateway.adapters import (
    FirestoreMailRepository,
    GCSRawMailStore,
    PubSubMailEventPublisher,
)
from mail_gateway.config import quota_policy_from_environment
from mail_gateway.domain import (
    InvalidRecipient,
    MailQuotaExceeded,
    UnknownRecipient,
    UnsafeMail,
)
from mail_gateway.operational_logging import (
    emit_gateway_event,
    safe_error_kind,
    trace_id_from_header,
)
from mail_gateway.service import MailGatewayService

MAX_RAW_MESSAGE_BYTES = 20 * 1024 * 1024
MAIL_PATH_PATTERN = re.compile(r"^/_ah/mail/(.+)$")


def build_service() -> MailGatewayService:
    project_id = os.environ["FOODLOG_MAIL_GCP_PROJECT_ID"]
    domain = os.environ["FOODLOG_MAIL_INBOUND_DOMAIN"]
    return MailGatewayService(
        domain=domain,
        repository=FirestoreMailRepository(
            project_id=project_id,
            domain=domain,
            quota_policy=quota_policy_from_environment(),
        ),
        object_store=GCSRawMailStore(
            project_id=project_id,
            bucket_name=os.environ["FOODLOG_MAIL_RAW_BUCKET"],
        ),
        event_publisher=PubSubMailEventPublisher(
            topic=os.environ["FOODLOG_MAIL_TOPIC"],
        ),
    )


class MailGatewayApplication:
    def __init__(self, service: MailGatewayService | None = None) -> None:
        self._service = service

    def __call__(
        self,
        environ: dict,
        start_response: Callable[[str, list[tuple[str, str]]], None],
    ) -> Iterable[bytes]:
        request_id = uuid4().hex
        trace_id = trace_id_from_header(environ.get("HTTP_X_CLOUD_TRACE_CONTEXT"))
        started = perf_counter()
        response_status = 500

        def tracked_start_response(
            status: str,
            headers: list[tuple[str, str]],
        ) -> None:
            nonlocal response_status
            response_status = int(status.split(" ", 1)[0])
            start_response(status, [*headers, ("X-Request-ID", request_id)])

        try:
            return self._handle(environ, tracked_start_response, request_id, trace_id)
        finally:
            emit_gateway_event(
                "INFO" if response_status < 500 else "ERROR",
                "http_request_completed",
                service="mail_gateway",
                environment=os.environ.get("GAE_ENV", "test"),
                request_id=request_id,
                trace_id=trace_id,
                http_method=environ.get("REQUEST_METHOD", "UNKNOWN"),
                http_route=(
                    "/_ah/mail/{recipient}"
                    if MAIL_PATH_PATTERN.fullmatch(environ.get("PATH_INFO", ""))
                    else "unmatched"
                ),
                http_status=response_status,
                duration_ms=max(0, round((perf_counter() - started) * 1_000)),
            )

    def _handle(
        self,
        environ: dict,
        start_response: Callable[[str, list[tuple[str, str]]], None],
        request_id: str,
        trace_id: str | None,
    ) -> Iterable[bytes]:
        if environ.get("REQUEST_METHOD") != "POST":
            return self._respond(start_response, "405 Method Not Allowed")
        match = MAIL_PATH_PATTERN.fullmatch(environ.get("PATH_INFO", ""))
        if match is None:
            return self._respond(start_response, "404 Not Found")
        try:
            content_length = int(environ.get("CONTENT_LENGTH") or "0")
        except ValueError:
            return self._respond(start_response, "400 Bad Request")
        if content_length < 1:
            return self._respond(start_response, "400 Bad Request")
        service = self._service or build_service()
        try:
            recipient = unquote(match.group(1))
            if content_length > MAX_RAW_MESSAGE_BYTES:
                service.record_attempt(recipient=recipient, size_bytes=content_length)
                raise UnsafeMail("message_too_large")
            raw_message = environ["wsgi.input"].read(MAX_RAW_MESSAGE_BYTES + 1)
            if not raw_message:
                return self._respond(start_response, "400 Bad Request")
            if len(raw_message) > MAX_RAW_MESSAGE_BYTES:
                service.record_attempt(recipient=recipient, size_bytes=len(raw_message))
                raise UnsafeMail("message_too_large")
            record = service.receive(recipient=recipient, raw_message=raw_message)
        except (InvalidRecipient, UnknownRecipient):
            emit_gateway_event(
                "WARNING",
                "inbound_mail_discarded",
                request_id=request_id,
                trace_id=trace_id,
                outcome="inactive_or_invalid_recipient",
            )
            return self._respond(start_response, "204 No Content")
        except UnsafeMail as error:
            emit_gateway_event(
                "WARNING",
                "inbound_mail_discarded",
                request_id=request_id,
                trace_id=trace_id,
                outcome=error.code,
            )
            return self._respond(start_response, "204 No Content")
        except MailQuotaExceeded as error:
            emit_gateway_event(
                "WARNING",
                "inbound_mail_discarded",
                request_id=request_id,
                trace_id=trace_id,
                outcome=error.code,
            )
            return self._respond(start_response, "204 No Content")
        except Exception as error:
            emit_gateway_event(
                "ERROR",
                "inbound_mail_failed",
                request_id=request_id,
                trace_id=trace_id,
                error_kind=safe_error_kind(error),
            )
            return self._respond(start_response, "503 Service Unavailable")
        emit_gateway_event(
            "INFO",
            "inbound_mail_published",
            request_id=request_id,
            trace_id=trace_id,
            account_id=record.account_id,
            mail_id=record.id,
            outcome=record.status,
        )
        return self._respond(start_response, "204 No Content")

    @staticmethod
    def _respond(
        start_response: Callable[[str, list[tuple[str, str]]], None],
        status: str,
    ) -> list[bytes]:
        start_response(
            status,
            [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", "0")],
        )
        return [b""]


app = MailGatewayApplication()
