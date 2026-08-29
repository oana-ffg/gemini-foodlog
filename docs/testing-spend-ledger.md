# Testing spend authorization and ledger

## Authorization

On 28 August 2026, Oana authorized Codex to use up to **DKK 200.000000** of
the project's promotional Google Cloud credits for FoodLog testing. This is a
cumulative ceiling for paid tests run on or after this authorization; it is not
a per-test allowance.

This authorization does not raise or replace the deployed **DKK 400** global
Gemini hard cap or the gross-spend billing alerts. It does not authorize Veo
except for the separately bounded probe below, public promotion, purchases, or
use of private images outside private testing.

### Separate Veo probe authorization

On 29 August 2026, Oana authorized exactly one privacy-safe eight-second Veo
3.1 Lite 720p video-only credit-coverage probe, with a maximum possible
out-of-pocket cost of **DKK 2.000000** and no automatic retry. The first request
was rejected before generation because Veo 3.1 prompt enhancement cannot be
disabled; no video was produced and its gross generation cost was **DKK
0.000000**. Oana then directly approved one exact corrected request that omitted
only that unsupported option and retained the same DKK 2 ceiling and disabled
retries. It produced eight generated seconds at a gross list-price cost of **DKK
1.540800**. This does not authorize the full Veo scenario set.

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
| 2026-08-28 22:06 CEST | Judge cat-control terminal-resume check | 0.000000 | 0.041811 | The existing exhausted cat event remained safely terminal at `attention_required`; no activity was published and the post-attempt production query found no new `model_usage_recorded` event. This verifies retry containment but does not constitute a fresh cat classification. |
| 2026-08-28 22:08 CEST | Fresh judge cat negative control | 0.283721 | 0.325532 | Two immutable production usage rows for event `524d2dc0-ebe8-4757-81b8-8eb9b1fd7e7e`: primary Gemini 3.6 Flash attempt failed after 34,523 tokens at DKK 0.244484; its single repair attempt failed after 4,261 tokens at DKK 0.039237. The event failed closed at `attention_required`; no activity or discard revision was published. |
| 2026-08-28 22:21 CEST | Post-v12 judge cat negative control | 0.546098 | 0.871630 | Two immutable production usage rows for revised event `524d2dc0-ebe8-4757-81b8-8eb9b1fd7e7e`: primary Gemini 3.6 Flash attempt failed after 37,232 tokens at DKK 0.269518; its bounded repair succeeded after 37,878 tokens at DKK 0.276580. The seeder then verified `likely_non_cooking`, wrote immutable not-cooking feedback, and reported one discarded entry. |
| 2026-08-28 23:45 CEST | Judge ambiguity v3 quota-project preflight | 0.000000 | 0.871630 | Firebase Admin rejected the ambient `ffutils` quota project before upload or inference. The operator seeder now supplies an explicit target-project quota credential, covered by a focused regression. No model-usage row was created. |
| 2026-08-28 23:47 CEST | Judge ambiguity v3 idempotent resume | 0.000000 | 0.871630 | The fixed seeder resumed the immutable attempt-three activity and correctly rejected its missing context-note citation. A post-attempt usage reconciliation found zero new rows, proving no paid provider call ran. |
| 2026-08-28 23:50 CEST | Judge ambiguity v4 semantic-guard control | 0.248385 | 1.120015 | One immutable successful `model_usage` row for event `1a9662d4-bc22-428b-8aae-6b120dea219d`: Gemini 3.6 Flash, prompt `food-event-v12`, 31,079 prompt plus 1,311 response tokens, 32,390 total, `actual_dkk_micros=248385`. The strict seeder accepted uncertain confidence, the focused candidate question, and the exact synthetic context-note citation. The judge account remains within its hard ceiling at 23 of 25 traces. |
| 2026-08-29 10:31 CEST | Authorized Veo 3.1 Lite credit-coverage probe | 0.000000 | 1.120015 | Exactly one eight-second 720p video-only request was submitted with SDK retries disabled. Vertex AI rejected the request before generation because Veo 3.1 prompt enhancement cannot be disabled. No video was produced, so no billable generation occurred. This separately authorized Veo probe does not consume the Gemini testing envelope. |
| 2026-08-29 10:36 CEST | Corrected Veo 3.1 Lite credit-coverage probe | 1.540800 | 1.120015 | After direct exact approval, one corrected request omitted only the unsupported prompt-enhancement option and retained disabled SDK retries. The completed operation produced exactly eight seconds of 1280-by-720 H.264 video at 24 fps; immutable output SHA-256 is `b61268b0182089872a063544fcd7a609ea5d6eae98f9c5469c72a1e495284c30`. Gross cost uses USD 0.03 per generated second and the recorded USD/DKK 6.42 rate. This separately authorized Veo probe does not consume the Gemini testing envelope; credit offset remains pending Cloud Billing settlement. |

## Current balance

- Authorized ceiling: **DKK 200.000000**
- Recorded spend since authorization: **DKK 1.120015**
- Remaining authorized testing budget: **DKK 198.879985**
- Separately authorized Veo probe gross spend: **DKK 1.540800** of **DKK 2.000000**
