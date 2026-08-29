# Hackathon credit expiry runbook

- **Status:** Required zero-out-of-pocket safety boundary
- **Last updated:** 2026-08-28
- **Promotion expiry:** 2026-09-24
- **Related decision record:** [mvp-architecture.md](mvp-architecture.md)

The FoodLog promotion showed DKK 984.25 remaining on 2026-08-25; that is an observation, not a current guaranteed balance. The project-level DKK 400 monthly budget measures gross spend before credits and sends current-spend alerts at DKK 100, 200, and 300.

## Important limitation

A Google Cloud budget sends notifications; it does not stop resources or cap the invoice. Budget reporting and notifications can also lag actual usage. The DKK 400 budget is therefore an early-warning envelope, not permission to spend DKK 400 and not a zero-cost guarantee.

## Current enforced state

- Production Gemini 3.6 Flash inference is enabled through Vertex AI and Google ADK.
- Every model workflow atomically reserves against a DKK 400 Gemini-only hard ceiling before its first provider call; deterministic and deployed rejection smokes verify the stop.
- The production API and workers use request-based Cloud Run, zero minimum instances, and one maximum instance.
- Public signup is enabled but transactionally capped at 25 accounts, each with a lifetime 200-image trial; explicitly configured internal and judge identities can be unlimited without bypassing the global model ceiling.
- The gross-spend budget remains an alert only. It cannot raise the application ceiling or authorize spend.
- The exact live hosted path and judging-period gap are recorded in [judge-availability-runbook.md](judge-availability-runbook.md).

## Completed model-processing gate

Oana explicitly selected the DKK 400 Gemini-only ceiling on 2026-08-26. Reservation, concurrent writers, retry idempotency, usage reconciliation, and deployed rejection above a temporary low ceiling are verified. Changing that amount still requires a fresh explicit decision; the Cloud Billing budget does not change it.

## Required before 2026-09-24

Perform a fresh inventory of billable resources and choose one explicitly approved outcome:

- attach replacement credit or an accepted personal operating budget; or
- stop/delete billable workloads and stored artifacts, then verify that no chargeable FoodLog resource remains; or
- disable billing for `gemini-foodlog-2026` after first checking the exact effect on retained data and services.

Do not automatically delete resources or disable billing without a reviewed inventory and Oana's explicit approval. Until one of those outcomes is verified, zero out-of-pocket spend after the promotional expiry cannot be guaranteed.

The binding rules currently put the judging period through 2026-10-01 at 11:45 PM Pacific Time, seven days after the known promotion expires, while Devpost's structured key-dates endpoint reports an earlier end. The official submission requirements make a hosted URL optional but strongly encouraged and explicitly say the app need not remain live at submission or judging when the demo and repository prove the Google Cloud deployment. Continuous judge availability is therefore an optional advantage, not an eligibility requirement. Revisit funding no later than 2026-09-23 and shut down safely if no post-credit spend is approved.
