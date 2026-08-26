# Gemini FoodLog

> A passive, uncertainty-aware food journal for discovering possible symptom triggers—without asking people to change how they cook.

Gemini FoodLog is a new project for Google's 2026 [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/). It explores whether an autonomous agent can reconstruct a useful household food timeline from ordinary, unstaged kitchen activity, sparse human clarification, and authorized grocery context.

## The problem

Food and symptom diaries can help people look for patterns, but keeping one manually is tedious and unreliable. The moments when a diary matters most are also the moments when remembering every ingredient and maintaining a logging habit may be hardest.

Most food-tracking products move the work around: photograph the plate, scan the barcode, weigh the portion, search for the dish, or type a description. This project starts from a stricter premise:

**The household changes absolutely nothing.**

No staged ingredients. No barcode ritual. No pre-meal app. No announcing what is being cooked.

## The concept

A fixed microcontroller camera observes the stove or counter through periodic images while the household behaves normally. An autonomous agent reasons across those observations over time rather than treating each frame as an isolated classification task.

It can ground its evolving hypothesis using authorized context such as recent grocery or recipe emails, earlier meals, known household patterns, and previous corrections. When an ambiguity materially affects the journal, it asks a focused question. The answer improves the current record and the household-specific knowledge used next time.

The output is a structured food timeline with evidence, uncertainty, and corrections intact. A person can separately record symptoms, allowing the system to surface cautious possible associations without pretending to diagnose a condition or prove causality.

```text
ordinary kitchen activity
          |
          v
periodic visual observations
          |
          v
evolving food-event hypothesis <--- authorized household context
          |
          +--- material uncertainty? ---> focused question
          |                                  |
          |<------------- learned answer ----+
          v
uncertainty-aware food journal
          |
          v
cautious exploration of possible symptom patterns
```

Calories and macronutrients may eventually be estimated from the same evidence, but they are secondary. The main product is a low-effort food-and-symptom journal.

## What makes this agentic

The hard problem is not recognizing a neatly presented ingredient. Real kitchen evidence is partial, temporal, and messy: hands obscure objects, packaging is turned away, an ingredient appears between frames, leftovers resemble a new meal, and multiple people share the space.

The intended system must therefore:

- operate in the background;
- decide whether observations belong to a food event;
- maintain and revise hypotheses across time;
- retrieve only relevant, authorized context;
- manage uncertainty and decide when a question is worth asking;
- learn household-specific patterns from corrections;
- persist a useful journal as an actual product action;
- compare later symptoms with the longitudinal record carefully.

That is a continuous observation, memory, decision, and action loop—not a chat interface wrapped around image recognition.

## Product principles

- **Zero behavior change:** ordinary, unstaged cooking is the test.
- **Sparse interaction:** ask only when the answer can materially improve the journal.
- **Learning must pay rent:** repeated household patterns should require fewer repeated questions.
- **Evidence over certainty theater:** preserve provenance, confidence, ambiguity, and corrections.
- **Health humility:** identify possible associations, never diagnoses or causal claims.
- **Privacy by design:** minimize collection and retention of household imagery, emails, and health data.
- **Measurable usefulness:** evaluate both reconstruction quality and the human effort required to obtain it.

## Hackathon fit

The MVP targets the **Taskmaster** track because it handles a personal, messy, multi-step workflow in the background and produces a maintained journal rather than merely generating text.

The official rules require every project to use:

- Gemini 3.5 or newer through the Gemini API or Vertex AI;
- at least one Google agent framework: Google ADK, GenAI SDK, Antigravity SDK, or Genkit;
- at least one Google Cloud infrastructure service.

See [AGENTS.md](./AGENTS.md) for the complete project contract and the hackathon checklist derived from the [official rules](https://allthingsagentichackathon.devpost.com/rules).

## Status

**Durable authenticated capture and event-level Gemini reasoning are live; the hosted product journal UI is still under construction.**

The production API runs on bounded, scale-to-zero Cloud Run and accepts JPEG or PNG captures through one shared contract. Verified browser users authenticate with Firebase; physical and Python cameras use independently revocable `FoodLogCamera` credentials. Accepted images consume the account's 200-image trial entitlement, persist in private Cloud Storage plus Firestore, deduplicate exact retries, and can be read back only through the authenticated owner API. The test account has exercised both browser-user and device upload paths with exact byte/hash read-back.

The React application is live at [gemini-foodlog-2026.web.app](https://gemini-foodlog-2026.web.app). Its separate `/camera` route provides the first manual phone-camera path; motion capture, wake lock, and the persistent browser delivery queue follow after the signed-in phone smoke. The locked [Python camera client](./clients/python/README.md) can already replay fixture sequences or capture a bounded webcam sequence through the production device-authenticated endpoint.

Account admission, the 25-account ceiling, 200-image trials, launch-mail consent records, the waitlist API, and one Pushover notification per new account are implemented. The API, image-processing worker, and notification worker are live.

Production groups captures into revisioned activity events and runs Gemini 3.6 Flash through Google ADK on Vertex AI. The deployed workflow loads the current tenant's ordered private images, emits a strict evidence-linked activity/meal hypothesis, reserves and records model spend, and persists redacted application-visible traces. The backend also has immutable correction, question, and household-learning primitives. A private scale-to-zero worker now classifies authenticated Nemlig confirmations and final invoices into one provenance-preserving purchase lifecycle; item/substitution normalization, longitudinal evaluation, and the corresponding real-data UI remain backlog work. The backlog evidence distinguishes deterministic tests from real Gemini runs.

See the working [MVP architecture and decision record](./docs/mvp-architecture.md), [credit-expiry runbook](./docs/credit-expiry-runbook.md), [source-controlled MVP backlog](./docs/mvp-backlog.html), and [historical preview record](./infra/preview/README.md). The original proposal is preserved verbatim in [CORE_IDEA.md](./CORE_IDEA.md).

The source-controlled [MVP backlog](./docs/mvp-backlog.html) lists every currently known build, deployment, testing, human-validation, and release task in intended phases with explicit dependencies. Mock and preview work is deliberately separated from production capabilities.

## Run the local slice

Requirements: Node.js 24+, npm, Python 3.12+, and [uv](https://docs.astral.sh/uv/).

```bash
npm install

cd services/backend
uv sync --all-groups
uv run uvicorn foodlog_backend.main:app --port 8080
```

In another terminal, from the repository root:

```bash
npm run dev:web
```

Open `http://127.0.0.1:5173`, sign in with a verified Firebase account, and open the dedicated camera page. Local API mode is for deterministic development tests; it does not invoke Gemini.

For production fixture or PC-webcam capture through the same device contract intended for firmware, use the locked [Python camera client](./clients/python/README.md).

## Verify

```bash
npm run typecheck
npm run build

cd services/backend
uv run ruff check .
uv run pytest

cd ../../clients/python
uv sync --frozen
uv run ruff check .
uv run pytest
```

## Success looks like

A convincing prototype should demonstrate an unedited sequence in which the household does nothing special, the agent reconstructs a food event from imperfect observations, uses context appropriately, asks only a necessary question, learns from the response, and updates an inspectable journal on Google Cloud.

The central evaluation question is simple:

**Can passive, unstaged observation produce a useful food journal with little enough human effort that people will actually keep using it?**
