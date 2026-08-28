# Provider data controls

Verified 28 August 2026 for the private Gemini FoodLog prototype.

## Product model path: Gemini on Vertex AI

- Production invokes `gemini-3.6-flash` in the Vertex AI `eu` multi-region through Google ADK. It does not use the consumer Gemini API, Grounding with Google Search or Maps, the Interactions API, or Gemini Live session resumption.
- Google's current service documentation says customer data is not used to train or fine-tune managed AI/ML models without the customer's prior permission or instruction.
- The same documentation distinguishes training from retention. Google may retain prompts for abuse monitoring under applicable terms. Google's published models also use project-isolated in-memory caching with a 24-hour TTL by default; the live FoodLog project returned the default cache configuration on 28 August 2026.
- Optional per-model request-and-response logging is disabled by default. A live configuration read for the configured model found no `PublisherModelConfig`, and Cloud Audit Logs contained no `PublisherModelConfig` mutation for this new project. The application and Terraform contain no request-response logging, BigQuery destination, Search/Maps grounding, or Interactions API configuration.
- FoodLog retains its own private, redacted application trace for debugging. That account-scoped trace is disclosed to the user and included in account export; it is not provider data sharing.

Authoritative references:

- [Gemini Enterprise Agent Platform and zero data retention](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/zero-data-retention)
- [Log and share requests and responses](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/request-response-logging)
- [Google Cloud Service Specific Terms](https://cloud.google.com/terms/service-terms)

## Codex operator debugging

- The current Codex account's Data controls page reported `Training is disabled` on 28 August 2026.
- Its separate `Include environments` switch was also off (`aria-checked=false`). This matters because OpenAI documents that the Codex full-environment control is separate from ordinary ChatGPT data controls.
- Do not submit feedback on a Codex task containing private FoodLog account data, because OpenAI documents that feedback can make the associated conversation eligible for model improvement even when training is otherwise disabled.

Authoritative reference: [How your data is used to improve model performance](https://help.openai.com/en/articles/5722486-how-your-data-is-used-to-improve-model-performance)

## Antigravity

Antigravity is not part of the deployed product path and has not received private FoodLog account content. Its specific account data controls have not been independently verified. Until they are, private FoodLog data must not be opened, pasted, attached, or queried through Antigravity. This fail-closed boundary is reflected in the signup notice.

## Signup disclosure

The web signup surface displays the complete `prototype-data-use-v1` notice before account creation and requires a separate acknowledgement. It discloses:

- what FoodLog stores;
- indefinite prototype retention until a fixed retention/deletion policy is added;
- Gemini processing through Vertex AI and the difference between no training and zero retention;
- optional Vertex request-response logging remaining disabled;
- narrowly scoped operator inspection with Codex for FoodLog debugging;
- the Antigravity prohibition until its controls are verified.

The launch-mail checkbox remains separate, optional, and unchecked by default.
