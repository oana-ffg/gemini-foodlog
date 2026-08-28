PARSER_VERSION = "nemlig-purchase-v1"
PDF_PROCESSOR_VERSION = "bounded-pdf-v1"
MAX_INVOICE_PDF_BYTES = 8 * 1024 * 1024
MAX_INVOICE_PDF_OBJECTS = 10_000
MAX_INVOICE_DECODED_STREAM_BYTES = 32 * 1024 * 1024
MAX_INVOICE_STREAM_EXPANSION_RATIO = 200
MAX_INVOICE_PDF_PAGES = 20
MAX_INVOICE_PAGE_DIMENSION_POINTS = 20_000
MAX_INVOICE_LAYOUT_OBJECTS_PER_PAGE = 50_000
MAX_INVOICE_LAYOUT_OBJECTS_TOTAL = 100_000
MAX_INVOICE_WORDS_TOTAL = 20_000
MAX_INVOICE_ROWS_TOTAL = 5_000
PDF_SUBPROCESS_ADDRESS_SPACE_BYTES = 192 * 1024 * 1024
PDF_SUBPROCESS_CPU_SOFT_SECONDS = 3
PDF_SUBPROCESS_CPU_HARD_SECONDS = 4
PDF_SUBPROCESS_WALL_SECONDS = 5
PDF_SUBPROCESS_OUTPUT_BYTES = 512 * 1024


class PurchasePdfRejected(ValueError):
    """A deterministic PDF or parser-budget rejection that must not be retried."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PurchasePdfIsolationFailure(RuntimeError):
    """An operational parser-isolation failure that is safe to retry."""
