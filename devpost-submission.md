# Gemini FoodLog

- **Hackathon:** Google All Things Agentic Hackathon 2026
- **Category:** The Taskmaster
- **Hosted application:** <https://gemini-foodlog-2026.web.app>
- **Repository:** <https://github.com/oana-ffg/gemini-foodlog>
- **Language:** English
- **Last reconciled with live Devpost requirements:** 2026-08-29

This file is the source-controlled submission copy. It deliberately contains no
password, private inbound-mail address, camera token, or private household data.
The dedicated judge credential belongs only in Devpost's private testing
instructions after the privacy-safe account is provisioned and verified.

## One-line Summary

Gemini FoodLog is an autonomous, uncertainty-aware food journal that reconstructs
meals from ordinary, unstaged kitchen activity. It observes imperfect camera
frames over time, combines them with authorized household context, asks only
focused questions, learns from corrections, and maintains an inspectable food
timeline on Google Cloud.

## Problem

Food and symptom diaries can help people investigate possible triggers, but
manual logging is burdensome and incomplete. Existing approaches still ask the
person to photograph a plate, scan packaging, weigh ingredients, search for a
dish, or type a description. FoodLog starts with a stricter requirement: the
household should not have to change how it cooks.

Real kitchen evidence is messy. A hand obscures an ingredient, a label faces
away, several low-quality frames belong to one event, leftovers resemble a new
meal, and a pet on the counter is activity but not cooking. The useful unit of
work is therefore not one image classification. It is a background workflow
that gathers evidence, maintains a revisable hypothesis, retrieves only relevant
context, decides when uncertainty deserves a question, and persists the result.

## Solution

FoodLog turns passive, ordinary kitchen observations into a revisable food
journal. It groups images over time, retrieves only relevant account context,
uses an agent to form an evidence-linked hypothesis, asks a focused question
only when it would materially help, and preserves corrections as future
household knowledge.

## Key Features

1. A signed-in phone camera, the Python capture client, or a revocable physical
   camera credential sends ordinary JPEG or PNG observations through one API.
2. The API enforces the account's image entitlement, stores the immutable image
   privately, persists metadata, and publishes a durable event.
3. Independent Cloud Run workers group captures into revisioned kitchen events
   and run a Google ADK agent with Gemini 3.6 Flash on Vertex AI.
4. The agent receives ordered private images plus bounded, account-scoped tools
   for relevant purchases, temporary context notes, household knowledge, and
   prior corrections. It emits an evidence-linked structured hypothesis rather
   than an unsupported label.
5. The web application shows the real image sequence, tentative or confirmed
   meal, uncertainty, observations, alternatives, context used, and complete
   revision history. The user can confirm it, correct a meal or component, or
   discard an event as not cooking.
6. Corrections remain in immutable history and can create versioned household
   knowledge. Separate longitudinal questions let the agent ask whether a
   repeated pattern is real without turning unidentified-meal questions into a
   generic inbox.
7. Optional authenticated Nemlig order and invoice emails become
   provenance-preserving purchase evidence. A person can also add time-bounded
   context such as an unusual ingredient expected tomorrow.
8. The account-data page exposes the records FoodLog holds and supports a
   private, temporary export. Stored evidence is tenant-scoped; browser clients
   cannot read Firestore or Cloud Storage directly.

The food timeline is intended to support later, cautious exploration of possible
food and symptom associations. FoodLog does not diagnose conditions or claim
causality. Calories are secondary to building a low-effort longitudinal record.

## Why This Matters

People investigating food-related symptoms need longitudinal evidence, but the
burden of manually maintaining a diary makes that evidence sparse precisely
when it matters. FoodLog aims to make the journal emerge from ordinary life
without asking the household to stage ingredients, scan products, or remember
to log every meal.

FoodLog completes a messy personal workflow instead of returning a chat answer.
It runs asynchronously, transforms unstructured multimodal observations into
durable state, calls bounded retrieval tools, handles retries and partial
failures, chooses whether human input is worth requesting, and updates a real
product record. Its core action is maintaining the journal over time.

## Architecture

- **Agent and model:** Google ADK, Google Gen AI SDK, Gemini 3.6 Flash, and the
  Vertex AI `eu` multi-region.
- **Google Cloud:** six scale-to-zero Cloud Run services, Pub/Sub with isolated
  subscriptions and dead-letter paths, Firestore, private Cloud Storage,
  Artifact Registry, Secret Manager, Cloud Logging, Cloud Monitoring, and an
  App Engine inbound-mail gateway.
- **User surface:** React, TypeScript, Vite, Firebase Hosting, and Firebase
  Authentication.
- **Capture clients:** protected browser manual/motion capture, a locked Python
  client for fixtures or webcams, and a portable C++20 physical-camera capture
  core targeting the M5Stack Unit CamS3-5MP.
- **Delivery and operations:** Terraform, keyless GitHub Workload Identity
  Federation, immutable container digests, protected deployment approval,
  bounded model-spend reservation, and source-controlled operational runbooks.

The [production architecture diagram](https://github.com/oana-ffg/gemini-foodlog/blob/main/docs/architecture-diagram.md)
shows the public, asynchronous, agentic, and tenant-data boundaries. The
[deployment guide](https://github.com/oana-ffg/gemini-foodlog/blob/main/docs/deployment.md)
contains the clean-checkout and cloud release procedure.

## How We Used AI

Gemini 3.6 Flash receives an ordered, bounded set of private kitchen images and
works through a Google ADK tool loop. The account-scoped tools retrieve only the
purchase evidence, temporary context, household knowledge, and prior corrections
relevant to the event. The model returns a validated structured hypothesis with
literal observations, alternatives, uncertainty, evidence references, and at
most one focused question. Deterministic application guards reject unsupported
certainty and preserve the original result plus every correction as immutable
history.

Veo 3.1 Lite produced one private synthetic evaluation probe. That probe is test
data, not a product feature, and FoodLog does not currently claim Veo as a
successfully integrated application model.

## How We Used Codex

Codex helped turn the initial product idea into an auditable build: architecture
and threat-boundary design, the sequenced HTML backlog, implementation across the
web, backend, mail, camera, Terraform, and CI packages, and repeated production
deployment and smoke testing. It also generated privacy-safe still fixtures,
constructed reproducible longitudinal evaluation manifests, reviewed failures
instead of retrying them into passes, ran focused and repository-wide security
reviews, and kept the submission claims tied to committed or deployed evidence.
Oana made the product, privacy, spending, publication, and external-submission
decisions; Codex did not silently grant itself those approvals.

## Authorized data sources

- kitchen images deliberately uploaded by the account's browser or camera;
- user corrections, pattern answers, temporary context notes, and explicitly
  taught household knowledge;
- authenticated Nemlig confirmation and final-invoice email evidence forwarded
  to an opaque per-account address;
- application-generated capture, grouping, inference, purchase, feedback,
  accounting, and revision metadata.

No browser route receives direct Firestore or Cloud Storage credentials. Gemini
receives only the bounded evidence selected for one account and one run. The
application stores a redacted operational trace; it does not expose hidden model
reasoning as a product claim.

## Findings and learnings

- **Time changes the problem.** A blurry ingredient can be ambiguous in one
  frame but useful when combined with what appeared immediately before and
  after it. Durable event grouping must precede meal reasoning.
- **Uncertainty needs product semantics.** A weak best guess, a genuinely unknown
  event, and non-cooking activity require different actions. A blanket “Looks
  right” button teaches the wrong thing.
- **Questions belong with their evidence.** Event-specific ambiguity stays on
  the matching timeline card. A separate agent-observation surface is useful for
  longitudinal hypotheses such as a recurring weekday breakfast pattern.
- **Learning needs provenance and scope.** “The next white meat may be duck” is
  temporary context; “this household often cooks steak in this basket” is a
  revisable household belief. Confirmation must not silently broaden either.
- **Retries are normal, not exceptional.** Images, Pub/Sub messages, model calls,
  feedback, and email all need stable identities, immutable revisions, bounded
  retry behavior, and inspectable failure states.
- **Cost controls must exist before autonomy.** Public accounts are capped at 25,
  public trials at 200 accepted images, Cloud Run at one instance per service,
  and every Gemini workflow reserves against a DKK 400 application ceiling
  before calling the provider.
- **Privacy changes the architecture.** Account identity is derived from verified
  authentication or server-owned events, object keys are server-generated,
  direct browser database access is denied, and cross-tenant negative tests are
  part of the release evidence.

## Known Limitations

- The physical-camera package contains a tested portable capture, motion,
  pacing, and retry core, but final board integration requires the selected
  hardware and real-device endurance testing.
- Manual phone capture works in the hosted app. Motion mode and offline queue
  recovery still require the planned multi-hour real-phone test.
- The live model path has passed bounded synthetic production smoke tests, but
  prompt quality and question frequency still need the planned multi-day
  ordinary-kitchen dataset and Oana's final normal-user evaluation.
- Purchase grounding currently targets Nemlig confirmation and invoice emails;
  it is optional and not a general retailer parser.
- The judge account credentials and provisioned production records are release
  artifacts and are not stored in this repository. The reviewed fixture bytes,
  hashes, and declarative seed manifest are source-controlled for auditability.
  Provisioning and automatic four-scenario verification are complete; the final
  judge-style browser login remains in the consolidated human test pass.
- The known promotional Google Cloud credit expires on 2026-09-24. Continuous
  hosted availability after that date depends on the explicit safety decision
  in the [judge availability runbook](https://github.com/oana-ffg/gemini-foodlog/blob/main/docs/judge-availability-runbook.md).
  The official requirements make a hosted URL optional but strongly encouraged
  and accept the public demo plus repository as deployment proof if the app is
  offline during judging.
- The product supports exploratory food/symptom journaling; it is not a medical
  device and does not make diagnostic or causal claims.

## Testing Instructions

### Public pre-login check

1. Open <https://gemini-foodlog-2026.web.app> in a fresh browser session.
2. Confirm the sign-in boundary loads over HTTPS.
3. Directly open [camera](https://gemini-foodlog-2026.web.app/camera),
   [context](https://gemini-foodlog-2026.web.app/context),
   [knowledge](https://gemini-foodlog-2026.web.app/knowledge),
   [purchases](https://gemini-foodlog-2026.web.app/purchases), and
   [account data](https://gemini-foodlog-2026.web.app/data). Each protected route
   should return the sign-in boundary rather than exposing account data.

### Authenticated workflow

The final Devpost testing field must include the verified dedicated judge
account's email and password. Those values must never be copied into this file,
the repository, screenshots, logs, or the public video.

After signing in with that dedicated account, use the read-only path below so
the same prepared evidence remains useful to every reviewer:

1. On **Your food timeline**, open one reviewed synthetic cooking event. Switch
   between its captured frames and inspect **Evidence and alternatives**,
   **Context used**, and **View revision history**.
2. Open the prepared red-meat event and inspect its correction revision, linked
   household-knowledge revision, and later real Gemini event that cites that
   exact learning. Leave **Correct it** untouched so the shared judge dataset
   stays deterministic.
3. Open **Discarded non-cooking activity**, select the reviewed synthetic cat
   event, and verify its original likely-non-cooking inference, immutable discard
   revision, image, and rationale. It must not appear in the food timeline.
4. Under **Patterns FoodLog wants you to check**, open the reviewed synthetic
   Thursday-steak question and inspect its five supporting meals, one chicken
   counterexample, date span, and uncertainty. Please leave it unanswered so
   later reviewers can inspect the same open question.
5. Open **Tell FoodLog something** and inspect the prepared time-bounded
   synthetic chicken-availability note and its visible validity window. No edit
   is required.
6. Open **Open the household wiki** and inspect the selected page's immutable
   revision history. No edit is required.
7. Open **Inspect purchase evidence** and read the optional forwarding workflow.
   The judge account intentionally contains no synthetic “authenticated Nemlig”
   record: fixture mail cannot honestly stand in for a real aligned DKIM result.
   Do not forward or upload a real email during judging.
8. Open **View all stored account data** and inspect the collection counts. If
   testing export, request it once, wait for completion, download the private
   archive, and sign out before sharing the browser.
9. Optional write test: open **Open the phone camera page** for one manual,
   non-private snapshot. Grant camera permission, register a browser camera,
   start it, use **Send snapshot**, pause it, then return to the journal and
   refresh. This adds a durable event to the shared judge account, so the
   prepared read-only workflow above is preferred. Do not start an unattended
   motion test during a short judge session.

REL-003 created the dedicated identity and loaded only reviewed synthetic
evidence. Its automatic production verification completed all four real-model
scenarios with no call-cap skips while preserving the prepared read-only state.
The remaining REL-003 check is a human judge-style browser login through the
steps above; the account and dataset already exist.

## Public Demo Link

<https://gemini-foodlog-2026.web.app>

## Public Repository Link

<https://github.com/oana-ffg/gemini-foodlog>

## Demo Video

The verified private draft is 198.033 seconds and remains pending Oana's final
viewing, frame-by-frame privacy/IP approval, and public YouTube or Vimeo upload.
No public video URL exists yet.

The rehearsed outline is: state the logging friction and value; show the
production architecture; run one continuous production capture-to-journal
sequence; show one correction becoming scoped learning; show the cat negative
control; show the longitudinal pattern question; prove the active Google Cloud
deployment; then close on the symptom-journal value. Exact timing, fallbacks,
and permitted claims live in
[the four-minute demo runbook](docs/demo-runbook.md).

## Screenshot Shot List

1. A degraded event on the food timeline showing the tentative guess, literal
   evidence, alternatives, and focused uncertainty question.
2. The immutable correction and household-learning revision history.
3. The cat-on-counter negative control in discarded non-cooking activity.
4. The longitudinal Thursday-steak pattern question and its supporting events.
5. Sanitized Google Cloud proof showing the deployed Cloud Run revision,
   immutable digest, Gemini 3.6 Flash configuration, and bounded runtime.

## Submission Readiness Notes

| Official requirement | FoodLog evidence | Final gate |
| --- | --- | --- |
| New project built during the 3–31 Aug 2026 submission period | First commit is 23 Aug 2026; REL-007 provenance, license, history, and secret audit is complete | Recheck the exact submitted commit. |
| Gemini 3.5 or newer plus a Google agent framework and Google Cloud infrastructure | Gemini 3.6 Flash through Google ADK/Gen AI SDK on Vertex AI; Cloud Run, Pub/Sub, Firestore, Storage, and other GCP services | Reconfirm the exact deployed release in REL-015. |
| One category | The Taskmaster | Reconfirm on the Devpost form. |
| Hosted URL optional but strongly encouraged; clear Google Cloud deployment proof required | Hosted Firebase URL plus judge account; the public demo and repository remain proof if billing-safe shutdown is chosen | Complete the consolidated human login and make the separate pre-expiry funding or shutdown decision. |
| English description covering features, technologies, data sources, findings, and learnings | This document | Fresh-reader and final-form review. |
| Repository URL and spin-up instructions | Public repository above; logged-out access plus clean-checkout README/deployment rehearsal passed | Recheck the exact submitted commit and public URL. |
| Architecture diagram | `docs/architecture-diagram.md` is implementation-audited and render-verified | Complete REL-017's committed upload-ready file and inspect it at submission scale. |
| Public demonstration video, no longer than four minutes, showing Google Cloud execution | REL-009 and REL-010 | Record, privacy-review, publish, and add the final URL. |
| English application and submission materials | English UI, documentation, test plan, and planned narration | Oana completes the final human language review. |
| Privacy and IP-safe media | Synthetic judge data and explicit media review tickets | REL-011 remains Oana's frame-by-frame publication gate. |

The authoritative requirements are the [official hackathon rules](https://allthingsagentichackathon.devpost.com/rules).

## TODO Official Form Fields

- **Submitter type:** Oana must choose Individuals or Organization. Organization
  is appropriate only if FoodLog is genuinely submitted on behalf of the
  incorporated entity.
- **Country of residence:** Denmark.
- **Category:** Taskmaster.
- **Organization name:** required by the live form only when submitting on
  behalf of an organization; do not invent a value for an individual entry.
- **Project start date:** 08-23-26, matching the first repository commit.
- **Repository:** <https://github.com/oana-ffg/gemini-foodlog>.
- **Reproducible README instructions:** Yes; the clean-checkout rehearsal and CI
  evidence are recorded in the backlog.
- **Hosted project:** <https://gemini-foodlog-2026.web.app>.
- **Private testing instructions:** paste the verified judge email and password
  from the secret store into Devpost's private field, followed by the read-only
  workflow above. Never put those credentials in this file.
- **Google SDK dropdown:** Agent Development Kit (ADK). The implementation also
  uses the Google Gen AI SDK.
- **Google Cloud service dropdown:** Cloud Run. The description additionally
  documents Pub/Sub, Firestore, Cloud Storage, App Engine, and other services.
- **Architecture diagram upload:** pending REL-017's upload-ready render.
- **Startup Excellence opt-in:** Oana must decide whether this is an eligible
  organization entry and, if so, provide the truthful incorporated name and
  corporate email in the private form.
- **Google AI models:** Gemini 3.6 Flash. Disclose the private Veo 3.1 Lite
  evaluation probe separately; do not claim it as a completed product
  integration unless that changes before submission.
- **Bonus content URL:** not available until REL-013 is approved and published.
- **Bonus social URL:** not available until REL-014 is approved and published.
- **Public video URL:** not available until REL-011 and REL-012 are complete.
