# Production Gemini model selection

Verified on 2026-08-26 for backlog item AI-001.

## Selected configuration

- Model: `gemini-3.6-flash`
- Launch stage: generally available
- Vertex location: `eu` multi-region
- Consumption: Standard PayGo
- SDK: Google Gen AI SDK through Vertex AI, API version `v1`
- Application configuration: `FOODLOG_MODEL=gemini-3.6-flash`,
  `GOOGLE_CLOUD_LOCATION=eu`, and `GOOGLE_GENAI_USE_VERTEXAI=true`

The stable model supports text and image input, structured output, function calling, and the
`eu` multi-region. This satisfies the hackathon requirement to use Gemini 3.5 or newer while
keeping model processing in the EU. The application does not use an AI Studio API key.

## Price boundary

Google's Standard PayGo price through 2026-12-31 for Gemini 3.6 Flash on a non-global endpoint
is USD 0.825 per million input tokens and USD 4.125 per million response-and-reasoning output
tokens. On 2027-01-01 it is scheduled to become USD 1.65 and USD 8.25 respectively. AI-012 must
use dated pricing configuration rather than treating today's introductory price as permanent.

The reproducible probe is deliberately text-only, caps output at 32 tokens, requests minimal
thinking, requires an explicit `--confirm-billable-probe` flag, verifies the exact response, and
prints server-reported token usage plus an estimated cost. It does not enable production
inference.

## Official evidence

- [All Things Agentic Hackathon rules](https://allthingsagentichackathon.devpost.com/rules)
- [Gemini 3.6 Flash model card](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-6-flash)
- [Google model pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)
- [Google Gen AI SDK overview](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/sdks/overview)

## Live probe

Terraform enabled `aiplatform.googleapis.com` in `gemini-foodlog-2026` with one addition and no
changes or deletions. The bounded live request then succeeded through Vertex AI in `eu`:

- exact response: `FOODLOG_AI001_OK`
- prompt tokens: 23
- response tokens: 9
- thinking tokens: 0
- total tokens: 32
- estimated cost at the current non-global Standard rate: USD 0.0000561

This proves the exact stable model and endpoint are callable with project billing. It does not
grant the worker access or enable production inference; those are separate backlog items.
