# Synthetic grocery longitudinal evaluation

## Outcome

EVAL-014 is complete as an evaluation. The production system processed three
deterministic 21-day histories from one versioned fixture set while the account
accumulated grocery orders and inferred meal events over time.

The strict scenario score was **5/12 (41.7%)** across the original history and
two fresh shifted replays. This is a measured reliability result, not a product
accuracy claim and not a reason to retry failures until they pass.

| Scenario | Original | Historical repeat 1 | Historical repeat 2 | Combined |
| --- | --- | --- | --- | ---: |
| Delivered steak grounding | Pass | Pass | Pass | 3/3 |
| Delivered duck substitution ambiguity | Pass | Fail closed | Pass | 2/3 |
| Removed-pork negative evidence | Fail | Fail | Fail | 0/3 |
| Order-only uncertainty | Fail | Fail | Fail closed | 0/3 |
| **Total** | **2/4** | **1/4** | **2/4** | **5/12** |

The dependable behavior in this corpus is uncertain steak grounding. Duck
grounding is promising but stochastic. Negative purchase evidence and
order-only evidence remain weak semantic signals. Those failures are retained
as evaluation findings for future model/prompt tuning; they do not invalidate
the ingestion, temporal-isolation, provenance, or fail-closed safety controls.

## Reproducibility and provenance

- Source manifest: `tests/fixtures/synthetic-grocery-evaluation.v1.json`
- Manifest SHA-256:
  `b81d5b2e0f1b8e25335e1c1ff058f55ebdbf15d89631efba4d31a4973fb7ba90`
- Datasets:
  `synthetic-grocery-longitudinal-v1`,
  `synthetic-grocery-longitudinal-v1-history-may`, and
  `synthetic-grocery-longitudinal-v1-history-june`
- The replay key and day shift deterministically derive unique dataset, order,
  invoice, capture, and idempotency identities while preserving fixture hashes
  and relative event timing.
- Every purchase is visibly marked synthetic and enters through the dedicated
  evaluation seeder. It never enters the authenticated retailer-email path.
- Event-time projections hid every purchase whose evidence arrived after the
  simulated capture. No future receipt or synthetic authentication claim was
  observed.

## Longitudinal growth exercised

The fresh repeats were inserted 56 and 28 days before the original history in
the same dedicated evaluation account. During the June replay, model-visible
purchase history grew from five to eight eligible purchase records and prior
inferred events grew from three to six. The runner scores each scenario once,
continues after semantic or terminal failures, and does not retry into a pass.

## Production evidence

- Real Gemini 3.6 Flash calls produced immutable model-usage rows and retained
  application-visible traces for primary and bounded repair attempts.
- Successful traces proved calls and responses for all seven required ADK
  context tools. Failed attempts were retained with explicit error evidence;
  the trace auditor now accepts a failed attempt without inventing a tool round
  trip while continuing to require tool evidence for successful attempts.
- Production workflow run `33264956665` deployed commit `ee12678` and completed
  its constrained image-only Terraform apply, exact-release smokes, protected
  tag verification, and final zero-drift check on immutable image digest
  `sha256:e216b1c56c2eade2142aa429e0140341dd68e8f6fcff9dd63fef2febb3ad19ee`.
- Full local backend validation passed: Ruff, 454 tests with 21 intentional
  environment skips, source distribution, and wheel build. Protected CI run
  `33264830880` passed all seven jobs on the same source commit.

## Cost

The grocery evaluation calls consumed **DKK 4.866692** of the authorized test
envelope. Total recorded authorized testing spend after all tests is **DKK
5.986707 of DKK 200**, leaving **DKK 194.013293**. The immutable per-run cost
evidence is recorded in `docs/testing-spend-ledger.md`.

## Interpretation limits

This corpus deliberately reuses four hash-bound visual fixtures with shifted
timestamps. It tests temporal growth, purchase-state interpretation, tool use,
provenance, and failure containment; it does not measure camera diversity,
household diversity, or real-world classification accuracy. Those belong to
the separate long-running and human evaluation tickets.
