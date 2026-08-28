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

## Current balance

- Authorized ceiling: **DKK 200.000000**
- Recorded spend since authorization: **DKK 0.000000**
- Remaining authorized testing budget: **DKK 200.000000**

