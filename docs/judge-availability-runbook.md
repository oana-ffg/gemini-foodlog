# Judge availability runbook

- **Last verified:** 2026-08-28
- **Hosted application:** <https://gemini-foodlog-2026.web.app>
- **Production health:** <https://foodlog-api-sptvo5nsga-ew.a.run.app/health>
- **Judging period ends:** 2026-10-01 at 11:45 PM Pacific Time
- **Promotional credit expires:** 2026-09-24

The official [hackathon rules](https://allthingsagentichackathon.devpost.com/rules) request a hosted project URL for judging and place the judging period after the promotional-credit expiry. This creates a real seven-day funding gap; it must not be hidden behind a budget alert or an assumption about free tiers.

The official Devpost MCP currently exposes conflicting dates: its structured key-dates endpoint says judging ends on 2026-09-25 at 00:00 UTC, while the binding rules still say 2026-10-01 at 11:45 PM Pacific Time. FoodLog deliberately uses the later rules date until Devpost resolves the discrepancy. The same official final-call announcement says the repository, video, and other linked materials should not change after the submission deadline until winners are announced around 2026-10-08; REL-018 tracks that separate artifact freeze.

## Verified hosted path

On 2026-08-28, a fresh signed-out browser session loaded the production Firebase Hosting site with no console warnings or errors. The root, `/camera`, `/context`, `/knowledge`, `/purchases`, and `/data` routes all resolved directly over HTTPS and exposed only the sign-in boundary. The production API health endpoint returned the production application rather than the historical preview. A privacy-safe judge identity and dataset are created separately under REL-003; credentials never enter source control or this document.

The deployed services use request-based Cloud Run billing with zero minimum and one maximum instance. Firebase Hosting has a no-cost allowance, and Cloud Run applies monthly compute/request allowances, but those allowances do not cover Gemini usage and cannot guarantee a zero invoice for regional retained storage, Artifact Registry, logging, or traffic. See the current [Cloud Run pricing](https://cloud.google.com/run/pricing) and [Firebase pricing](https://firebase.google.com/pricing).

## Availability controls

- Firebase Hosting serves the static React application independently of backend releases.
- Six Cloud Run services and six retained smoke jobs use immutable Artifact Registry digests.
- `protected-active` and `protected-rollback` tags preserve the current and previous release digests.
- Every service has zero minimum instances and one maximum instance.
- Public admission is capped at 25 accounts; each public trial is capped at 200 accepted images.
- Every model workflow atomically reserves against the DKK 400 Gemini-only hard ceiling before calling Vertex AI.
- The DKK 400 gross Cloud Billing budget alerts at DKK 100, 200, and 300 but is not a hard stop.
- The [credit-expiry runbook](credit-expiry-runbook.md) owns the mandatory pre-expiry decision.

## Required timeline

1. **Through 2026-09-23:** keep the hosted path monitored, preserve the judge account and reviewed synthetic dataset, and keep all changes inside the existing hard controls.
2. **No later than 2026-09-23:** re-read the live promotional balance and billing resources. Oana must explicitly choose replacement credit, a personal operating envelope, or shutdown/disable-billing actions.
3. **If replacement credit is attached:** verify its exact amount and expiry, keep the DKK 400 application ceiling unless Oana explicitly changes it, and smoke the judge path after the billing change.
4. **If no post-2026-09-24 funding is approved:** stop before the credit expires and follow the reviewed shutdown option. Do not claim the project will remain fully testable through 2026-10-01.
5. **On 2026-10-01:** run a signed-out URL check and a judge-account read-only smoke before the judging window closes.

## Current blocker

Full zero-out-of-pocket availability through the end of judging is not yet guaranteed because the known promotion ends seven days earlier. REL-002 remains blocked until replacement credit or another explicit post-expiry operating decision is verified. This does not block REL-003, documentation, the architecture diagram, or other release preparation.
