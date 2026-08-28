# Four-minute demo runbook

- **Target duration:** 3:35–3:50
- **Hard limit:** 4:00
- **Language:** English
- **Primary scenario:** ambiguous distant meat preparation with relevant,
  explicitly synthetic recent chicken-availability context
- **Negative control:** cat on an otherwise inactive counter
- **Data boundary:** dedicated judge/demo account containing only reviewed
  synthetic evidence unless Oana explicitly approves a real clip for publication

This runbook selects the strongest honest story the current product can support.
It is not permission to publish any image, account, trace, or recording. REL-011
remains the frame-by-frame privacy and IP gate.

## The story in one sentence

FoodLog turns an ordinary, poor-angle kitchen observation into a durable,
evidence-linked food event; uses a time-bounded synthetic availability note
without treating it as proof; asks one useful question when the distinction
matters; learns from the answer; and correctly keeps obvious non-cooking activity
out of the journal.

## Why this scenario

The distant ambiguous-meat fixture captures the actual product difficulty:
someone opens a package by the sink, the air-fryer basket is nearby, and meat
colour is barely visible from roughly two metres away. A reviewed, time-bounded
synthetic note says chicken is available in the kitchen that week; this makes
chicken relevant but does not prove that the pictured pack is chicken. A useful
agent should preserve that distinction, make a tentative guess, explain its
evidence and alternatives, and ask a focused chicken-versus-red-meat question
rather than “What meal were you cooking?”

The cat fixture is the negative control requested for the product. It proves the
system has an explicit non-cooking path and preserves reversible evidence rather
than forcing every kitchen motion into a meal.

## Required prepared state

Before recording, REL-003 must provide a dedicated account containing only
reviewed synthetic data:

- a time-bounded synthetic note stating that chicken is known to be available in
  the kitchen that week;
- the ambiguous distant-meat capture and its completed real Gemini/ADK run;
- the red-meat follow-up capture used after the correction;
- the cat-on-counter capture and its completed real Gemini/ADK run;
- one evidence-backed synthetic longitudinal pattern question;
- one versioned household-knowledge page created from reviewed feedback;
- no real household image, email, name, address, order identifier, forwarding
  address, camera secret, account identifier, or private trace payload.

The model outputs must be reviewed before recording. If the ambiguous-meat run
does not make a concrete tentative guess, preserve the run as evaluation evidence
but do not pretend it demonstrates the intended behavior. Re-run only under the
approved model-evaluation budget and record the new immutable revision.

## Recording layout

- Record at 1920×1080 or higher with the browser at a readable zoom.
- Hide bookmarks, personal browser chrome, notifications, terminal history,
  account email, forwarding address, URLs containing identifiers, and any secret.
- Use the hosted Firebase application, not a local mock.
- Keep one sanitized terminal ready for the exact fixture upload and one sanitized
  Google Cloud view ready for backend proof.
- Preload pages, but refresh the journal during the unedited action segment so the
  resulting durable state is visibly retrieved from production.
- Record the upload-to-journal action as one continuous segment. The final video
  may be edited around it, but must not splice a fabricated result into that run.

## Timed shot list

### 0:00–0:25 — The friction and value

**Picture:** one poor-angle kitchen frame beside the hosted journal.

**Narration:**

> Food and symptom diaries are useful only if people keep them. Most trackers
> still ask you to photograph a plate, scan something, or type what you ate.
> Gemini FoodLog watches ordinary kitchen activity instead. It reconstructs a
> food timeline, preserves uncertainty, and learns when you correct it.

Keep the health claim narrow: the journal can support later investigation of
possible associations; it does not diagnose or establish causality.

### 0:25–0:45 — Show the production architecture

**Picture:** the rendered architecture diagram, zoomed to the core path.

**Narration:**

> A phone, Python camera, or physical camera credential sends images through one
> authenticated Cloud Run API. Pub/Sub decouples grouping from inference. A
> Google ADK agent runs Gemini 3.6 Flash on Vertex AI with bounded tools for this
> account's purchases, notes, knowledge, and prior feedback. Firestore and private
> Cloud Storage hold the durable, tenant-scoped record.

### 0:45–1:55 — Unedited proof of action

**Picture:** begin with a sanitized terminal and continue without a cut through
the hosted journal result.

1. Replay the reviewed ambiguous fixture through the production Python camera
   client using its revocable demo camera credential.
2. Show the successful API acceptance with no token or account identifier visible.
3. Show the corresponding Cloud Run/Pub/Sub processing status changing from
   accepted to grouped and analysed. Wait rather than cutting across the action.
4. Refresh **Your food timeline** and open the resulting event.
5. Switch between source frames if the event contains more than one.
6. Expand **Evidence and alternatives** and **Context used**.
7. Point out that the displayed synthetic availability note makes chicken
   relevant but is not treated as visual proof. Show the tentative best guess,
   explicit uncertainty, and focused question if the reviewed run produced them.

Do not narrate exact model wording before it is visibly loaded. If processing
does not finish inside the segment's rehearsal envelope, use the fallback path
below and say that the shown immutable run was recorded earlier from the same
production release.

### 1:55–2:35 — Correct once, learn once

**Picture:** the same timeline card, correction controls, and revision history.

1. Use **Correct it** with a reviewed synthetic correction that identifies the
   meat and explains the reusable cue without broadening it into an absolute rule.
2. Show the appended feedback revision and preserved original inference.
3. Open **Open the household wiki** and show the versioned, scoped learning.
4. Show the reviewed red-meat follow-up event and exactly what context the agent
   used. If the follow-up did not improve, say so; do not claim learning accuracy
   from one example.

**Narration:**

> Feedback never overwrites history. It appends a revision and can propose a
> scoped household belief. Temporary context stays temporary; repeated evidence
> can strengthen a pattern, and contradictions remain visible.

### 2:35–2:55 — Negative control

**Picture:** cat-on-counter event and non-cooking history.

1. Show the cat fixture's reviewed Gemini result.
2. Use **Discard as not cooking** if the event is still provisional.
3. Show it under **Discarded non-cooking activity** with its image and revision
   history still available.

Do not show **Looks right** on a genuinely unknown event. The intended UI permits
confirmation only for a concrete tentative/confirmed guess.

### 2:55–3:15 — Longitudinal behavior

**Picture:** **Patterns FoodLog wants you to check**.

Show one synthetic multi-week observation with supporting examples,
counterexamples, uncertainty, and its narrow confirm/correct actions. Explain
that event-specific questions remain on the matching timeline entry; this feed
is for hypotheses such as a recurring weekday breakfast pattern.

### 3:15–3:38 — Visible Google Cloud proof

**Picture:** sanitized Google Cloud console or read-only command output showing:

- the exact Cloud Run service revision handling the recorded run;
- a successful inference execution or structured application log;
- Vertex AI model/version evidence;
- Firestore/Storage resource presence without opening private payloads.

**Narration:**

> This is the production release on Google Cloud: Cloud Run, Pub/Sub, Firestore,
> private Storage, Google ADK, and Gemini 3.6 Flash on Vertex AI. Services scale to
> zero, accounts and trials are capped, and every model run reserves against an
> application-level spend ceiling before the provider call.

### 3:38–3:50 — Close

**Picture:** journal plus one-line product principle.

**Narration:**

> The goal is not certainty theatre. It is a useful food record with less human
> effort, clear provenance, and fewer repeated questions as FoodLog learns the
> household.

Leave at least ten seconds of margin before the four-minute limit.

## Fallback evidence

The video must not depend on a live model response arriving on cue.

Prepare a privacy-reviewed recording of the exact production upload and its
completed immutable result before the final take. If the live run is delayed:

1. keep the failed/delayed attempt honest and show its processing status;
2. cut after the unedited attempt, not inside it;
3. show the pre-recorded completed run with its capture ID, model invocation, and
   service revision visually redacted but internally cross-checked;
4. narrate that it is a prior run from the same immutable production release;
5. never substitute local fixture output or hand-authored UI data.

Also export still evidence for the final editor:

- accepted capture response with secrets removed;
- processing status progression;
- complete timeline card and image viewer;
- context and alternatives;
- correction and revision history;
- scoped knowledge revision;
- cat discard and discarded history;
- pattern question evidence;
- Cloud Run revision and Vertex AI invocation proof.

## Permitted and forbidden claims

### Safe claims when the matching shot is present

- Production images and metadata are tenant-scoped and privately persisted.
- Grouping and inference are separate asynchronous workers.
- The shown run used Google ADK and Gemini 3.6 Flash on Vertex AI.
- The agent used only the context displayed for that event.
- Feedback appended a revision and produced the displayed scoped learning.
- The cat event was removed from the food timeline without erasing its evidence.
- The project has deterministic security, retry, quota, and accounting tests.

### Claims not supported yet

- FoodLog accurately identifies meals in general or reaches a numerical accuracy
  threshold.
- It has already reduced Oana's question burden over weeks of ordinary use.
- The physical microcontroller client is deployed and endurance-tested.
- Browser motion mode is safe for unattended multi-hour use on every phone.
- Purchase grounding supports retailers other than the implemented Nemlig shapes.
- The system diagnoses food triggers, allergies, intolerances, or any condition.
- Credits, free tiers, budget alerts, or the DKK 400 model ceiling guarantee a
  zero invoice through the entire judging period.

## Rehearsal checklist

- [ ] REL-003 judge/demo account contains only reviewed synthetic evidence.
- [ ] The exact production release is immutable, healthy, and not waiting on an
      older deployment gate.
- [ ] The main Gemini run and cat control have reviewed application-visible traces.
- [ ] The correction text and knowledge scope are approved before typing them.
- [ ] The pattern question has real synthetic supporting and counterexample state.
- [ ] The sanitized production upload command succeeds without revealing secrets.
- [ ] One unedited upload-to-durable-result take is captured.
- [ ] Every Google Cloud shot belongs to the same release/run story.
- [ ] Narration plus pauses times at no more than 3:50.
- [ ] Oana completes REL-011 frame-by-frame before any upload.

The final timed rehearsal cannot be marked complete until the deployed judge
dataset, long-run evaluation evidence, and Oana's normal-user findings are
available. This document finishes the autonomous scenario-selection and recording
plan without inventing those results.
