# Hackathon credit expiry runbook

- **Status:** Required zero-out-of-pocket safety boundary
- **Last updated:** 2026-08-25
- **Promotion expiry:** 2026-09-24
- **Related decision record:** [mvp-architecture.md](mvp-architecture.md)

The FoodLog promotion currently has DKK 984.25 remaining. The project-level DKK 400 monthly budget measures gross spend before credits and sends current-spend alerts at DKK 100, 200, and 300.

## Important limitation

A Google Cloud budget sends notifications; it does not stop resources or cap the invoice. Budget reporting and notifications can also lag actual usage. The DKK 400 budget is therefore an early-warning envelope, not permission to spend DKK 400 and not a zero-cost guarantee.

## Current safe state

- Gemini and every other external model call are disabled.
- The private Cloud Run preview scales to zero and is capped at one instance.
- Public signup is disabled.
- No workflow is allowed to enable model processing merely because promotional credit exists.

## Required before model processing

1. Implement an application-level global model-spend kill switch using recorded model usage and conservative worst-case reservation before each call.
2. Keep the enforceable threshold materially below the then-current promotional balance to allow for reporting lag, in-flight work, retries, and non-model infrastructure.
3. Re-confirm the amount with Oana immediately before enabling the model; do not infer it from the DKK 400 alert budget.
4. Verify the kill switch with deterministic tests and a deployed rejection smoke test.

## Required before 2026-09-24

Perform a fresh inventory of billable resources and choose one explicitly approved outcome:

- attach replacement credit or an accepted personal operating budget; or
- stop/delete billable workloads and stored artifacts, then verify that no chargeable FoodLog resource remains; or
- disable billing for `gemini-foodlog-2026` after first checking the exact effect on retained data and services.

Do not automatically delete resources or disable billing without a reviewed inventory and Oana's explicit approval. Until one of those outcomes is verified, zero out-of-pocket spend after the promotional expiry cannot be guaranteed.
