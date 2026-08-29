from hashlib import sha256

from foodlog_backend.purchase_pdf_evaluation import build_hostile_corpus, run_soak


def test_hostile_pdf_corpus_is_deterministic_and_unique() -> None:
    first = build_hostile_corpus(mutation_count=5)
    second = build_hostile_corpus(mutation_count=5)

    assert [case.name for case in first] == [case.name for case in second]
    assert [sha256(case.payload).hexdigest() for case in first] == [
        sha256(case.payload).hexdigest() for case in second
    ]
    assert len({sha256(case.payload).digest() for case in first}) == len(first)
    assert {case.name for case in first} >= {
        "geometry_budget",
        "invalid_structure",
        "object_budget",
        "oversized_input",
        "page_budget",
        "stream_expansion_budget",
    }
    assert all(
        case.expected_codes is None
        for case in first
        if case.name.startswith("mutated_structure_")
    )


def test_hostile_pdf_corpus_recovers_across_repeated_attempts() -> None:
    report = run_soak(rounds=2, mutation_count=2, max_total_seconds=30)

    assert report["passed"] is True
    assert report["corpus_cases"] == 8
    assert report["total_attempts"] == 16
    assert all(
        sum(result["code_counts"].values()) == 2 for result in report["results"]
    )
