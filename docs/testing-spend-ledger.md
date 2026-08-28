# Testing spend authorization and ledger

## Authorization

On 28 August 2026, Oana authorized Codex to use up to **DKK 200.000000** of
the project's promotional Google Cloud credits for FoodLog testing. This is a
cumulative ceiling for paid tests run on or after this authorization; it is not
a per-test allowance.

This authorization does not raise or replace the deployed **DKK 400** global
Gemini hard cap or the gross-spend billing alerts. It does not authorize Veo,
public promotion, purchases, or use of private images outside private testing.

Operational rules:

- Record the measured incremental cost and new cumulative total after every
  paid test.
- Check the cumulative total before starting another paid test and fail closed
  if its conservative reservation could exceed DKK 200.
- Use persisted application model-usage records as the primary evidence for
  Gemini costs. Record other billable test resources from authoritative Cloud
  Billing data when available.
- Never subtract, rewrite, or hide failed attempts. Corrections are new ledger
  rows that explain the discrepancy.
- Stop paid testing and tell Oana immediately if measured or newly reconciled
  testing spend exceeds DKK 200.

## Ledger

| Recorded at (Europe/Copenhagen) | Test | Incremental cost (DKK) | Authorized cumulative (DKK) | Evidence |
| --- | --- | ---: | ---: | --- |
| 2026-08-28 | Authorization baseline | 0.000000 | 0.000000 | New DKK 200 testing envelope begins at this authorization. Earlier project usage remains preserved in the application and Cloud Billing ledgers but is outside this newly authorized envelope. |
| 2026-08-28 21:46 CEST | Judge ambiguity control preflight | 0.000000 | 0.000000 | Identity Toolkit rejected the local ADC quota-project selection before upload or inference. A post-attempt production log query found no new `model_usage_recorded` event. No paid model test ran. |
| 2026-08-28 21:48 CEST | Judge ambiguity control authentication preflight | 0.000000 | 0.000000 | The shell expanded the temporary Firebase-key variable before applying its one-command environment assignment, so sign-in failed before upload or inference. No model test ran. |
| 2026-08-28 21:49 CEST | Judge ambiguity control, historical-schema failure | 0.000000 | 0.000000 | Production accepted the resumable upload, then rejected an older persisted unsafe hypothesis while assembling API/agent context. The failure happened before provider invocation; the model-usage query returned no new record. The compatibility boundary was fixed without changing immutable history. |
| 2026-08-28 22:01 CEST | Judge ambiguity control, post-deploy resume | 0.000000 | 0.000000 | The deployed compatibility fix made the existing activity readable. The idempotent resume completed in 0.732 seconds and the acceptance check rejected that historical activity as overconfident; no new `model_usage_recorded` event exists after the release, so no paid provider call ran. |
| 2026-08-28 22:03 CEST | Fresh judge ambiguity release control | 0.041811 | 0.041811 | Persisted production `model_usage_recorded` event for event `811a98ae-514e-41c8-a832-8323a964c426`: Gemini 3.6 Flash, 4,191 tokens, primary attempt succeeded, `actual_dkk_micros=41811`. The result was safely uncertain and included a focused candidate question, but the judge acceptance check rejected it because it did not cite the synthetic availability note. |

## Current balance

- Authorized ceiling: **DKK 200.000000**
- Recorded spend since authorization: **DKK 0.041811**
- Remaining authorized testing budget: **DKK 199.958189**
