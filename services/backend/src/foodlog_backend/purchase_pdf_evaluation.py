from __future__ import annotations

import argparse
import json
import random
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256

from .purchase_pdf_isolation import parse_invoice_pdf_isolated
from .purchase_pdf_limits import (
    MAX_INVOICE_PDF_BYTES,
    MAX_INVOICE_PDF_OBJECTS,
    PDF_SUBPROCESS_WALL_SECONDS,
    PurchasePdfIsolationFailure,
    PurchasePdfRejected,
)


@dataclass(frozen=True)
class CorpusCase:
    name: str
    payload: bytes
    expected_codes: frozenset[str]


def _build_pdf(objects: list[bytes], *, root_object_id: int = 1) -> bytes:
    output = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode())
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {root_object_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


def _page_pdf(
    *,
    page_count: int = 1,
    width: int = 612,
    height: int = 792,
    extra_objects: int = 0,
    extra_stream: bytes | None = None,
    valid_table_header: bool = False,
) -> bytes:
    page_ids = list(range(3, 3 + page_count))
    font_id = 3 + page_count
    content_id = font_id + 1
    page_suffix = ""
    if valid_table_header:
        page_suffix = (
            f" /Resources << /Font << /F1 {font_id} 0 R >> >>"
            f" /Contents {content_id} 0 R"
        )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{' '.join(f'{value} 0 R' for value in page_ids)}] "
            f"/Count {page_count} >>"
        ).encode(),
    ]
    objects.extend(
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}]"
            f"{page_suffix} >>"
        ).encode()
        for _ in page_ids
    )
    if valid_table_header:
        labels = (
            (20, "Varekategori"),
            (105, "Enhed"),
            (155, "Indregnet"),
            (220, "rabat"),
            (265, "Stk."),
            (310, "pris"),
            (350, "Antal"),
            (405, "Pris"),
        )
        content = "\n".join(
            f"BT /F1 8 Tf 1 0 0 1 {x} 700 Tm ({label}) Tj ET" for x, label in labels
        ).encode()
        objects.extend(
            [
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                b"<< /Length "
                + str(len(content)).encode()
                + b" >>\nstream\n"
                + content
                + b"\nendstream",
            ]
        )
    objects.extend(b"<< >>" for _ in range(extra_objects))
    if extra_stream is not None:
        compressed = zlib.compress(extra_stream, level=9)
        objects.append(
            b"<< /Length "
            + str(len(compressed)).encode()
            + b" /Filter /FlateDecode >>\nstream\n"
            + compressed
            + b"\nendstream"
        )
    return _build_pdf(objects)


def _mutated_structure_cases(count: int) -> list[CorpusCase]:
    if count < 0:
        raise ValueError("mutation count must be non-negative")
    baseline = _page_pdf()
    generator = random.Random(0xF00D2026)
    cases: list[CorpusCase] = []
    hashes: set[str] = set()
    while len(cases) < count:
        payload = bytearray(baseline)
        mutation_kind = len(cases) % 3
        if mutation_kind == 0:
            cutoff = generator.randrange(8, len(payload) - 1)
            del payload[cutoff:]
        elif mutation_kind == 1:
            index = generator.randrange(5, len(payload))
            payload[index] ^= generator.randrange(1, 256)
        else:
            index = generator.randrange(5, len(payload) - 8)
            payload[index : index + 8] = b"00000000"
        digest = sha256(payload).hexdigest()
        if digest in hashes:
            continue
        hashes.add(digest)
        cases.append(
            CorpusCase(
                name=f"mutated_structure_{len(cases) + 1:02d}",
                payload=bytes(payload),
                expected_codes=frozenset({"pdf_structure_rejected"}),
            )
        )
    return cases


def build_hostile_corpus(*, mutation_count: int) -> tuple[CorpusCase, ...]:
    cases = [
        CorpusCase(
            name="invalid_structure",
            payload=b"%PDF-1.7\ntruncated",
            expected_codes=frozenset({"pdf_structure_rejected"}),
        ),
        CorpusCase(
            name="oversized_input",
            payload=b"%PDF-" + b"0" * MAX_INVOICE_PDF_BYTES,
            expected_codes=frozenset({"pdf_bytes_exceeded"}),
        ),
        CorpusCase(
            name="object_budget",
            payload=_page_pdf(extra_objects=MAX_INVOICE_PDF_OBJECTS - 2),
            expected_codes=frozenset({"pdf_object_count_exceeded"}),
        ),
        CorpusCase(
            name="page_budget",
            payload=_page_pdf(page_count=21, valid_table_header=True),
            expected_codes=frozenset({"pdf_page_count_exceeded"}),
        ),
        CorpusCase(
            name="geometry_budget",
            payload=_page_pdf(width=20_001, height=20_001),
            expected_codes=frozenset({"pdf_page_geometry_exceeded"}),
        ),
        CorpusCase(
            name="stream_expansion_budget",
            payload=_page_pdf(extra_stream=b"0" * (1024 * 1024)),
            expected_codes=frozenset({"pdf_stream_expansion_exceeded"}),
        ),
    ]
    cases.extend(_mutated_structure_cases(mutation_count))
    return tuple(cases)


def run_soak(
    *,
    rounds: int,
    mutation_count: int,
    max_total_seconds: float,
) -> dict[str, object]:
    if rounds < 1:
        raise ValueError("rounds must be positive")
    if max_total_seconds <= 0:
        raise ValueError("maximum total seconds must be positive")
    corpus = build_hostile_corpus(mutation_count=mutation_count)
    started = time.monotonic()
    results: dict[str, dict[str, object]] = {}
    total_attempts = 0
    for round_index in range(rounds):
        for case in corpus:
            if time.monotonic() - started > max_total_seconds:
                raise RuntimeError("hostile PDF soak exceeded its total wall budget")
            attempt_started = time.monotonic()
            try:
                parse_invoice_pdf_isolated(case.payload)
            except PurchasePdfRejected as error:
                code = error.code
            except PurchasePdfIsolationFailure as error:
                raise RuntimeError(
                    f"{case.name} caused retryable parser isolation failure in round "
                    f"{round_index + 1}: {error}"
                ) from error
            else:
                raise RuntimeError(
                    f"{case.name} unexpectedly produced a purchase document in round "
                    f"{round_index + 1}"
                )
            duration = time.monotonic() - attempt_started
            if code not in case.expected_codes:
                raise RuntimeError(
                    f"{case.name} returned {code!r}, expected "
                    f"{sorted(case.expected_codes)!r}"
                )
            if duration > PDF_SUBPROCESS_WALL_SECONDS + 1:
                raise RuntimeError(
                    f"{case.name} exceeded the parent recovery envelope: {duration:.3f}s"
                )
            result = results.setdefault(
                case.name,
                {
                    "bytes": len(case.payload),
                    "code_counts": Counter(),
                    "max_seconds": 0.0,
                    "sha256": sha256(case.payload).hexdigest(),
                },
            )
            counts = result["code_counts"]
            assert isinstance(counts, Counter)
            counts[code] += 1
            result["max_seconds"] = max(float(result["max_seconds"]), duration)
            total_attempts += 1
    elapsed = time.monotonic() - started
    serialized_results = []
    for name, result in sorted(results.items()):
        counts = result["code_counts"]
        assert isinstance(counts, Counter)
        serialized_results.append(
            {
                **result,
                "code_counts": dict(sorted(counts.items())),
                "max_seconds": round(float(result["max_seconds"]), 6),
                "name": name,
            }
        )
    return {
        "corpus_cases": len(corpus),
        "elapsed_seconds": round(elapsed, 6),
        "mutation_count": mutation_count,
        "passed": True,
        "rounds": rounds,
        "schema_version": "hostile-pdf-soak-v1",
        "total_attempts": total_attempts,
        "results": serialized_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a deterministic hostile-PDF soak against the isolated invoice parser."
    )
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--mutations", type=int, default=16)
    parser.add_argument("--max-total-seconds", type=float, default=300)
    args = parser.parse_args()
    report = run_soak(
        rounds=args.rounds,
        mutation_count=args.mutations,
        max_total_seconds=args.max_total_seconds,
    )
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
