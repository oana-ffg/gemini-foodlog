from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable
from urllib.parse import unquote

from mail_gateway.adapters import (
    FirestoreMailRepository,
    GCSRawMailStore,
    PubSubMailEventPublisher,
)
from mail_gateway.domain import InvalidRecipient, UnknownRecipient
from mail_gateway.service import MailGatewayService

LOGGER = logging.getLogger(__name__)
MAX_RAW_MESSAGE_BYTES = 20 * 1024 * 1024
MAIL_PATH_PATTERN = re.compile(r"^/_ah/mail/(.+)$")


def build_service() -> MailGatewayService:
    project_id = os.environ["FOODLOG_MAIL_GCP_PROJECT_ID"]
    return MailGatewayService(
        domain=os.environ["FOODLOG_MAIL_INBOUND_DOMAIN"],
        repository=FirestoreMailRepository(project_id=project_id),
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
        if content_length > MAX_RAW_MESSAGE_BYTES:
            return self._respond(start_response, "413 Payload Too Large")
        raw_message = environ["wsgi.input"].read(MAX_RAW_MESSAGE_BYTES + 1)
        if not raw_message or len(raw_message) > MAX_RAW_MESSAGE_BYTES:
            return self._respond(start_response, "413 Payload Too Large")
        try:
            (self._service or build_service()).receive(
                recipient=unquote(match.group(1)),
                raw_message=raw_message,
            )
        except (InvalidRecipient, UnknownRecipient):
            LOGGER.warning("Discarded inbound mail for an inactive or invalid recipient")
            return self._respond(start_response, "204 No Content")
        except Exception:
            LOGGER.exception("Inbound mail persistence or publication failed")
            return self._respond(start_response, "503 Service Unavailable")
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
