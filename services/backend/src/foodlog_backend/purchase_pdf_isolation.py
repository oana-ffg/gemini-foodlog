from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from typing import Any

from .models import ParsedPurchaseDocument
from .purchase_pdf_limits import (
    MAX_INVOICE_PDF_BYTES,
    PARSER_VERSION,
    PDF_SUBPROCESS_OUTPUT_BYTES,
    PDF_SUBPROCESS_WALL_SECONDS,
    PurchasePdfIsolationFailure,
    PurchasePdfRejected,
)


def parse_invoice_pdf_isolated(pdf: bytes) -> ParsedPurchaseDocument:
    if not pdf or len(pdf) > MAX_INVOICE_PDF_BYTES:
        raise PurchasePdfRejected("pdf_bytes_exceeded")
    if os.name != "posix":
        raise PurchasePdfIsolationFailure("pdf_isolation_requires_posix")

    command = [sys.executable, "-I", "-m", "foodlog_backend.purchase_pdf_worker"]
    child_environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                env=child_environment,
                start_new_session=True,
            )
            try:
                process.communicate(input=pdf, timeout=PDF_SUBPROCESS_WALL_SECONDS)
            except subprocess.TimeoutExpired as error:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise PurchasePdfRejected("pdf_wall_time_exceeded") from error
            stdout.seek(0, os.SEEK_END)
            output_size = stdout.tell()
            if output_size > PDF_SUBPROCESS_OUTPUT_BYTES:
                raise PurchasePdfRejected("pdf_output_exceeded")
            stdout.seek(0)
            output = stdout.read(PDF_SUBPROCESS_OUTPUT_BYTES + 1)
    except PurchasePdfRejected:
        raise
    except OSError as error:
        raise PurchasePdfIsolationFailure("pdf_subprocess_unavailable") from error

    if process.returncode == -signal.SIGXCPU:
        raise PurchasePdfRejected("pdf_cpu_limit_exceeded")
    if process.returncode == -signal.SIGXFSZ:
        raise PurchasePdfRejected("pdf_output_exceeded")
    if process.returncode != 0:
        raise PurchasePdfIsolationFailure("pdf_subprocess_failed")
    try:
        envelope: Any = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PurchasePdfIsolationFailure("pdf_subprocess_protocol_invalid") from error
    if not isinstance(envelope, dict):
        raise PurchasePdfIsolationFailure("pdf_subprocess_protocol_invalid")
    if envelope.get("status") == "rejected" and isinstance(envelope.get("code"), str):
        raise PurchasePdfRejected(envelope["code"])
    if envelope.get("status") != "ok" or not isinstance(envelope.get("document"), dict):
        raise PurchasePdfIsolationFailure("pdf_subprocess_protocol_invalid")
    try:
        document = ParsedPurchaseDocument.model_validate(envelope["document"])
    except ValueError as error:
        raise PurchasePdfIsolationFailure("pdf_subprocess_document_invalid") from error
    if document.parser_version != PARSER_VERSION:
        raise PurchasePdfIsolationFailure("pdf_subprocess_version_mismatch")
    return document
