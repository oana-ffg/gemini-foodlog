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

The project is expected to target the **Taskmaster** track because it handles a personal, messy, multi-step workflow in the background and produces a maintained journal rather than merely generating text. Final track selection and architecture are still open decisions.

The official rules require every project to use:

- Gemini 3.5 or newer through the Gemini API or Vertex AI;
- at least one Google agent framework: Google ADK, GenAI SDK, Antigravity SDK, or Genkit;
- at least one Google Cloud infrastructure service.

See [AGENTS.md](./AGENTS.md) for the complete project contract and the hackathon checklist derived from the [official rules](https://allthingsagentichackathon.devpost.com/rules).

## Status

**Concept and constraints defined; architecture not yet selected.**

This first commit intentionally contains no implementation and makes no deployment, integration, or accuracy claims. The next step is an explicit architecture discussion covering the camera-to-cloud boundary, event model, agent framework, persistence and memory, clarification channel, privacy policy, evaluation harness, and the smallest convincing end-to-end demo.

Reproducible setup instructions will be added with the first runnable implementation. Until then, there is nothing to install or run.

## Success looks like

A convincing prototype should demonstrate an unedited sequence in which the household does nothing special, the agent reconstructs a food event from imperfect observations, uses context appropriately, asks only a necessary question, learns from the response, and updates an inspectable journal on Google Cloud.

The central evaluation question is simple:

**Can passive, unstaged observation produce a useful food journal with little enough human effort that people will actually keep using it?**
