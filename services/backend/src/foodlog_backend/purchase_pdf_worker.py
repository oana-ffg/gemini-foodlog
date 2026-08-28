from __future__ import annotations

import json
import os
import resource
import sys

from .purchase_pdf_limits import (
    MAX_INVOICE_PDF_BYTES,
    PDF_SUBPROCESS_ADDRESS_SPACE_BYTES,
    PDF_SUBPROCESS_CPU_HARD_SECONDS,
    PDF_SUBPROCESS_CPU_SOFT_SECONDS,
    PDF_SUBPROCESS_OUTPUT_BYTES,
)


def _apply_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (PDF_SUBPROCESS_CPU_SOFT_SECONDS, PDF_SUBPROCESS_CPU_HARD_SECONDS),
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (PDF_SUBPROCESS_OUTPUT_BYTES, PDF_SUBPROCESS_OUTPUT_BYTES),
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform.startswith("linux"):
        resource.setrlimit(
            resource.RLIMIT_AS,
            (PDF_SUBPROCESS_ADDRESS_SPACE_BYTES, PDF_SUBPROCESS_ADDRESS_SPACE_BYTES),
        )


def _emit(envelope: dict[str, object]) -> None:
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > PDF_SUBPROCESS_OUTPUT_BYTES:
        payload = b'{"code":"pdf_output_exceeded","status":"rejected"}'
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        _apply_limits()
    except (OSError, ValueError):
        return os.EX_OSERR
    pdf = sys.stdin.buffer.read(MAX_INVOICE_PDF_BYTES + 1)
    if not pdf or len(pdf) > MAX_INVOICE_PDF_BYTES:
        _emit({"status": "rejected", "code": "pdf_bytes_exceeded"})
        return 0
    try:
        from .purchase_normalization import parse_invoice_pdf
        from .purchase_pdf_limits import PurchasePdfRejected

        document = parse_invoice_pdf(pdf)
    except PurchasePdfRejected as error:
        _emit({"status": "rejected", "code": error.code})
        return 0
    except MemoryError:
        _emit({"status": "rejected", "code": "pdf_memory_limit_exceeded"})
        return 0
    except ValueError:
        _emit({"status": "rejected", "code": "pdf_structure_rejected"})
        return 0
    except Exception:
        return os.EX_SOFTWARE
    _emit({"status": "ok", "document": document.model_dump(mode="json")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
