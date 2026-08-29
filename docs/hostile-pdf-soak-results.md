# Hostile PDF isolation soak

## Result

`SEC-002` passed its final deployed long test on 29 August 2026.

- **220 of 220** hostile-PDF attempts ended in a bounded terminal rejection.
- **0** attempts produced a purchase document.
- **0** attempts produced a retryable parser-isolation failure.
- **0** model calls were made.
- The deterministic corpus contained **22** unique PDFs and ran for **10**
  rounds, starting a fresh isolated parser child for every attempt.
- Application-measured soak time was **212.418533 seconds**. The complete Cloud
  Run execution envelope was **275.315 seconds**.

## Deployed evidence

- Project: `gemini-foodlog-2026`
- Region: `europe-west1`
- Execution: `foodlog-context-tools-smoke-b8nxc`
- Immutable production digest:
  `sha256:60c08e90aa3092ac1805afc8d3a1c7fa200441f056ffe0a2ac1330c7201e92fe`
- Runtime: 1 vCPU, 512 MiB, zero retries
- Structured report schema: `hostile-pdf-soak-v1`
- Completion: successful at `2026-08-29T17:46:31.221143Z`

The soak used a one-execution argument and timeout override on the existing
Terraform-managed context-tools smoke job. Readback after completion proved the
persistent job remained `python -m foodlog_agent.context_tools_smoke`, with its
120-second timeout, zero retries, one vCPU, and 512 MiB limit unchanged.

## Corpus and terminal outcomes

| Case family | Cases | Attempts | Terminal result |
| --- | ---: | ---: | --- |
| Invalid/truncated PDF | 1 | 10 | `pdf_structure_rejected` |
| Oversized input | 1 | 10 | `pdf_bytes_exceeded` |
| Object-count budget | 1 | 10 | `pdf_object_count_exceeded` |
| Page-count budget | 1 | 10 | `pdf_page_count_exceeded` |
| Page-geometry budget | 1 | 10 | `pdf_page_geometry_exceeded` |
| Decoded-stream expansion budget | 1 | 10 | `pdf_stream_expansion_exceeded` |
| Deterministic random structural mutations | 16 | 160 | 140 `pdf_structure_rejected`; 20 `pdf_page_count_invalid` |

Every fixed-budget case returned its exact expected code in all ten rounds.
Random structural mutations accept any safe `PurchasePdfRejected` code because
damage can deterministically fail at different validation layers; all sixteen
mutations returned the same safe code in every round. The slowest individual
attempt was 1.224097 seconds, below the six-second parent recovery envelope.

The corpus generator, hashes, exact-code assertions, fresh-child runner, total
wall budget, and per-attempt recovery budget are versioned in
`foodlog_backend.purchase_pdf_evaluation`. Unit coverage proves the corpus is
deterministic and unique and repeats every case without leaking parser state.

## Harness correction retained as evidence

The first deployed attempt, `foodlog-context-tools-smoke-6kd2n`, stopped safely
when one random mutation returned `pdf_page_count_invalid` instead of the
harness's overly narrow `pdf_structure_rejected` expectation. This was not a
parser-isolation failure: the PDF was rejected before normalization and no
purchase, mail, authentication, or model state changed. The harness was
corrected to require any bounded `PurchasePdfRejected` outcome for random
mutations while retaining exact-code checks for the six deliberately constructed
budget cases. The failed attempt remains in the spend ledger.

## Cost

The successful execution's conservative gross list-price estimate is **DKK
0.033583**. This prices its entire 275.315-second Cloud Run envelope at one vCPU
and 0.5 GiB, before the Cloud Run monthly free tier, promotional credits, or
invoice settlement. Together with the retained first attempt, authorized test
spend is now **DKK 6.027609 of DKK 200.000000**. The spend ledger remains the
canonical source for cost accounting.

## Interpretation boundary

This demonstrates deterministic recovery from the bounded corpus under the
deployed Linux container and configured worker-sized resources. It does not
claim that arbitrary future parser/library vulnerabilities are impossible. The
production path continues to rely on aligned-mail authentication, unique
attachment selection, fresh child-process isolation, Linux resource limits,
parent wall-time termination, terminal rejection durability, and retry/DLQ
behavior as independent layers.
