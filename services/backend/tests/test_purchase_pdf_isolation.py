import signal
import subprocess
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import pytest

from foodlog_backend import purchase_normalization
from foodlog_backend.purchase_normalization import parse_final_invoice
from foodlog_backend.purchase_pdf_isolation import parse_invoice_pdf_isolated
from foodlog_backend.purchase_pdf_limits import (
    MAX_INVOICE_PDF_BYTES,
    MAX_INVOICE_PDF_OBJECTS,
    MAX_INVOICE_PDF_PAGES,
    MAX_INVOICE_STREAM_EXPANSION_RATIO,
    MAX_INVOICE_WORDS_TOTAL,
    PARSER_VERSION,
    PDF_SUBPROCESS_OUTPUT_BYTES,
    PurchasePdfIsolationFailure,
    PurchasePdfRejected,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nemlig"


def test_legitimate_invoice_parses_inside_bounded_subprocess() -> None:
    parsed = parse_final_invoice(
        (FIXTURES / "final-invoice.eml").read_bytes(),
        invoice_reference="9000000001",
    )

    assert parsed.parser_version == PARSER_VERSION
    assert len(parsed.items) == 2
    assert len(parsed.charges) == 5


def test_parser_version_remains_compatible_with_existing_normalizations() -> None:
    assert PARSER_VERSION == "nemlig-purchase-v1"


def test_invoice_parser_ignores_preceding_nonmatching_pdf_attachment() -> None:
    message = BytesParser(policy=policy.default).parsebytes(
        (FIXTURES / "final-invoice.eml").read_bytes()
    )
    wrong_attachment = EmailMessage()
    wrong_attachment.set_content(
        b"%PDF-invalid-preceding-attachment",
        maintype="application",
        subtype="pdf",
        cte="base64",
    )
    wrong_attachment.add_header(
        "Content-Disposition",
        "attachment",
        filename="Faktura - other.pdf",
    )
    payload = message.get_payload()
    assert isinstance(payload, list)
    payload.insert(0, wrong_attachment)

    parsed = parse_final_invoice(
        message.as_bytes(),
        invoice_reference="9000000001",
    )

    assert len(parsed.items) == 2


def test_invalid_and_oversized_pdf_are_terminal_rejections() -> None:
    with pytest.raises(PurchasePdfRejected, match="pdf_structure_rejected"):
        parse_invoice_pdf_isolated(b"%PDF-not-a-valid-document")
    with pytest.raises(PurchasePdfRejected, match="pdf_bytes_exceeded"):
        parse_invoice_pdf_isolated(b"%PDF-" + b"0" * MAX_INVOICE_PDF_BYTES)


def test_wall_timeout_kills_the_parser_process_group(monkeypatch) -> None:
    killed: list[tuple[int, signal.Signals]] = []

    class TimedOutProcess:
        pid = 4321
        returncode: int | None = None

        def communicate(self, *, input: bytes, timeout: int):
            del input
            raise subprocess.TimeoutExpired(["pdf-worker"], timeout)

        def wait(self) -> int:
            self.returncode = -signal.SIGKILL
            return self.returncode

    def fake_popen(*args, **kwargs):
        del args
        assert kwargs["start_new_session"] is True
        assert set(kwargs["env"]) == {"PATH", "PYTHONHASHSEED", "PYTHONIOENCODING"}
        return TimedOutProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        "foodlog_backend.purchase_pdf_isolation.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    with pytest.raises(PurchasePdfRejected, match="pdf_wall_time_exceeded"):
        parse_invoice_pdf_isolated(b"%PDF-timeout")
    assert killed == [(4321, signal.SIGKILL)]


def test_unexpected_child_failure_remains_retryable(monkeypatch) -> None:
    class FailedProcess:
        returncode = 70

        def communicate(self, *, input: bytes, timeout: int):
            del input, timeout
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FailedProcess())

    with pytest.raises(PurchasePdfIsolationFailure, match="pdf_subprocess_failed"):
        parse_invoice_pdf_isolated(b"%PDF-operational-failure")


def test_parent_rejects_child_output_above_protocol_cap(monkeypatch) -> None:
    class VerboseProcess:
        returncode = 0

        def __init__(self, output) -> None:
            self.output = output

        def communicate(self, *, input: bytes, timeout: int):
            del input, timeout
            self.output.write(b"x" * (PDF_SUBPROCESS_OUTPUT_BYTES + 1))
            self.output.flush()
            return None

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: VerboseProcess(kwargs["stdout"]),
    )

    with pytest.raises(PurchasePdfRejected, match="pdf_output_exceeded"):
        parse_invoice_pdf_isolated(b"%PDF-output-limit")


class FakePdf:
    def __init__(self, *, object_count: int = 0, objects: dict[int, object] | None = None) -> None:
        class Xref:
            def get_objids(self):
                return range(object_count)

        values = objects or {}
        self.doc = type(
            "Document",
            (),
            {
                "xrefs": [Xref()],
                "getobj": lambda self, object_id: values[object_id],
            },
        )()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        del args


def header_words() -> list[dict[str, float | str]]:
    labels = [
        "Varekategori",
        "Enhed",
        "Indregnet",
        "rabat",
        "Stk.",
        "pris",
        "Antal",
        "Pris",
    ]
    return [
        {"text": label, "top": 10.0, "x0": index * 60.0, "x1": index * 60.0 + 40.0}
        for index, label in enumerate(labels)
    ]


def test_object_and_page_tree_caps_precede_unbounded_layout_work(monkeypatch) -> None:
    monkeypatch.setattr(
        purchase_normalization.pdfplumber,
        "open",
        lambda *args, **kwargs: FakePdf(object_count=MAX_INVOICE_PDF_OBJECTS + 1),
    )
    with pytest.raises(PurchasePdfRejected, match="pdf_object_count_exceeded"):
        purchase_normalization.parse_invoice_pdf(b"%PDF-object-limit")

    monkeypatch.setattr(
        purchase_normalization.pdfplumber,
        "open",
        lambda *args, **kwargs: FakePdf(),
    )
    monkeypatch.setattr(
        purchase_normalization.PDFPage,
        "create_pages",
        lambda document: iter([object()] * (MAX_INVOICE_PDF_PAGES + 1)),
    )

    class FakePage:
        width = 600
        height = 800

        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.objects: dict[str, list] = {}

        def extract_words(self):
            return header_words()

    monkeypatch.setattr(purchase_normalization, "Page", FakePage)
    with pytest.raises(PurchasePdfRejected, match="pdf_page_count_exceeded"):
        purchase_normalization.parse_invoice_pdf(b"%PDF-page-limit")


def test_word_cap_rejects_before_row_materialization(monkeypatch) -> None:
    monkeypatch.setattr(
        purchase_normalization.pdfplumber,
        "open",
        lambda *args, **kwargs: FakePdf(),
    )
    monkeypatch.setattr(
        purchase_normalization.PDFPage,
        "create_pages",
        lambda document: iter([object()]),
    )

    class WordHeavyPage:
        width = 600
        height = 800

        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.objects: dict[str, list] = {}

        def extract_words(self):
            word = {"text": "x", "top": 10.0, "x0": 0.0, "x1": 1.0}
            return [word] * (MAX_INVOICE_WORDS_TOTAL + 1)

    monkeypatch.setattr(purchase_normalization, "Page", WordHeavyPage)
    with pytest.raises(PurchasePdfRejected, match="pdf_word_count_exceeded"):
        purchase_normalization.parse_invoice_pdf(b"%PDF-word-limit")


def test_stream_expansion_is_explicitly_bounded(monkeypatch) -> None:
    class ExpandingStream:
        rawdata = b"x" * 10

        def get_data(self) -> bytes:
            return b" " * (10 * MAX_INVOICE_STREAM_EXPANSION_RATIO + 1)

    monkeypatch.setattr(purchase_normalization, "PDFStream", ExpandingStream)
    monkeypatch.setattr(
        purchase_normalization.pdfplumber,
        "open",
        lambda *args, **kwargs: FakePdf(
            object_count=1,
            objects={0: ExpandingStream()},
        ),
    )

    with pytest.raises(PurchasePdfRejected, match="pdf_stream_expansion_exceeded"):
        purchase_normalization.parse_invoice_pdf(b"%PDF-expansion-limit")
