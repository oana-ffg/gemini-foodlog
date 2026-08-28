# Building Gemini FoodLog: an agent that turns ordinary kitchen activity into a food journal

> I created this article about Gemini FoodLog for the purpose of entering Google's
> 2026 All Things Agentic Hackathon. The project itself was also created during the
> hackathon's 2026 submission period.

Keeping a food diary sounds simple until you have to do it every day. The diary
is most useful when it is detailed and consistent; the person who needs it is
often tired, busy, symptomatic, or already halfway through cooking. Existing
tools usually move the work rather than remove it: photograph the plate, scan a
barcode, weigh an ingredient, search for a dish, or type what happened.

Gemini FoodLog starts with a deliberately difficult product constraint:

**The household should change absolutely nothing about how it cooks.**

The system observes ordinary kitchen activity through periodic camera images,
reconstructs food events over time, combines visual evidence with authorized
household context, and maintains an uncertainty-aware journal. When a missing
detail matters, it asks one focused question. When the user corrects it, the
correction becomes durable, scoped household knowledge rather than disappearing
into chat history.

The long-term goal is a low-effort food record that can support cautious
investigation of possible symptom associations. It is not a diagnostic system,
and calories are not the defining problem.

## The agentic problem is temporal, not just visual

A polished photo of a plated meal is an image-recognition problem. A fixed
kitchen camera sees something else:

- a hand blocking the package;
- a label facing the wall;
- a blurry glimpse of pale or red meat from two metres away;
- an empty air-fryer basket followed by an ingredient and then a closed drawer;
- leftovers that resemble a new meal;
- a cat jumping onto the counter.

No single frame necessarily contains the answer. FoodLog first groups ordered
captures into a revisioned activity event. Only then does the inference worker
ask what is happening, what food is likely involved, what alternatives remain,
and whether a human answer would materially improve the record.

That distinction shaped the whole architecture. Capture acceptance, event
grouping, model inference, feedback, and learning have separate durable states.
A slow model call cannot lose the accepted image. A Pub/Sub retry cannot create
a second meal. A correction cannot erase the original inference.

## The production path on Google Cloud

The hosted React application uses Firebase Authentication and Firebase Hosting.
A signed-in phone can send a manual snapshot or run local motion detection; a
locked Python client replays reviewed fixtures or captures from a webcam; and a
portable C++20 capture core implements the state, pacing, queue, and retry
semantics for the selected microcontroller camera.

All capture clients use one Cloud Run API contract. Browser requests carry a
verified Firebase ID token. Device clients use independently revocable
`FoodLogCamera` credentials. The server derives the account from authentication;
clients do not choose an account path.

After entitlement and content checks, the API stores the image in a private
Cloud Storage bucket, writes tenant-scoped metadata to Firestore, and publishes
a capture event. Independent Pub/Sub subscriptions invoke separate Cloud Run
grouping and inference workers with Google-signed OIDC tokens. Each stream has
bounded retries and a retained dead-letter path.

The inference worker runs a Google ADK agent using Gemini 3.6 Flash through the
Google Gen AI SDK on Vertex AI's `eu` multi-region. The agent receives ordered
images plus bounded tools for the current account's relevant purchases,
temporary context notes, household knowledge, and previous corrections. It must
return a structured, evidence-linked activity hypothesis. The application stores
a redacted operational trace, usage accounting, and an immutable inference
revision.

Optional Nemlig order and invoice emails enter through an App Engine inbound-mail
gateway. The gateway preserves raw MIME privately; a separate worker verifies the
mail shape and authentication before creating normalized purchase revisions.
Purchase evidence can make chicken a relevant hypothesis, but it is never treated
as proof that a blurry package contains chicken.

## Making uncertainty visible in the product

An early interface mistake exposed an important design rule. It showed
“Unrecognized kitchen activity” beside a “Looks right” button and duplicated meal
questions in a generic clarification inbox. The controls existed, but the
semantics did not.

The corrected product distinguishes three cases:

1. A concrete tentative guess can be confirmed or corrected.
2. A genuinely unknown event can be classified, but cannot be confirmed as a
   meaningful answer.
3. Non-cooking activity can be discarded from the food timeline while retaining
   its evidence and revision history.

Event-specific ambiguity now stays on the matching timeline card. A separate
agent-observation section is reserved for longitudinal hypotheses: “You usually
eat steak on Thursdays—is that accurate?” is useful after enough evidence;
“What meal were you cooking?” belongs with the event that caused the uncertainty.

The journal shows the original image sequence at a stable size with zoom, the
tentative or confirmed result, confidence, observations, alternatives, context
used, a focused question when warranted, corrections, and complete revision
history. A cat-on-counter fixture exercises the explicit non-cooking route.

## Learning without turning a guess into a household fact

Household learning needs both provenance and scope.

“My mother-in-law brought duck and we intend to cook it tomorrow” is temporary
context with a validity window. “This basket by the sink often means steak” may
be a reusable but revisable household belief. “Thursday dinner is always steak”
is not supported merely because three Thursdays contained steak.

FoodLog therefore separates temporary notes, pattern observations, and versioned
household knowledge. A proposed learning links to raw evidence. Confirmation can
strengthen only the supported claim; it cannot silently broaden the wording or
applicability. Contradictions append new revisions instead of mutating history.
Exact retries use stable identities, so the same feedback cannot create duplicate
knowledge.

## Privacy, isolation, and cost are part of the agent design

Kitchen images, purchase emails, and symptom-related records are sensitive. The
browser never receives direct Firestore or Cloud Storage access. Security Rules
deny direct clients, object keys are server-generated and account-scoped, buckets
use public-access prevention, and every API read derives its tenant from trusted
identity. Negative tests exercise unauthenticated access, forged account scope,
cross-tenant reads, worker invocation, and camera revocation.

Autonomy also needs a financial boundary. Cloud Run services have zero minimum
instances and one maximum instance. Public admission is transactionally capped at
25 accounts, with 200 accepted images per public trial. Every Gemini workflow
atomically reserves against a DKK 400 application-level model ceiling before the
provider call. A separate Cloud Billing budget alerts at DKK 100, 200, and 300,
but the project documentation is explicit that an alert is not a spending cap or
a zero-invoice guarantee.

Deployment is keyless through GitHub Workload Identity Federation. Terraform
defines narrow plan and deployment identities, immutable Artifact Registry
digests, protected production approval, post-deploy smoke evidence, and rollback
tags. The repository's live HTML backlog records whether each task is in progress,
waiting for deployment, awaiting quick/long/human testing, blocked, or done—with
the evidence needed to justify the state.

## What synthetic tests can and cannot prove

The privacy-safe fixture set includes clear steak, chicken, and leftover-pasta
frames; degraded distant red, pale, and deliberately ambiguous meat views; and a
cat jumping onto an inactive counter. The project also has scenario contracts
that state the expected observations, safe alternatives, forbidden conclusions,
and when a question is justified.

These fixtures are excellent for regression testing data contracts, UI behavior,
retry semantics, uncertainty handling, and real Gemini/ADK integration. They do
not establish real-world meal-classification accuracy. The project will not claim
a statistically meaningful accuracy number from a small self-created dataset.

The remaining evaluation is intentionally concrete: multi-hour phone motion and
offline recovery, the physical board integration, multi-day ordinary-kitchen
data, prompt/question-frequency tuning, and a final normal-user trial. The demo
runbook separates already-verified Cloud execution from those still-open quality
questions.

## What I learned

The most important lessons were architectural rather than prompt-shaped:

- **Group before reasoning.** The model needs an event, not an isolated JPEG.
- **Context is evidence, not truth.** A recent purchase changes the hypothesis
  space; it does not identify the object.
- **A question must earn its interruption.** Ask only when the answer changes the
  journal or produces reusable learning.
- **Corrections need immutable history.** Learning is not trustworthy if the
  original guess disappears.
- **Retries are a normal operating mode.** Stable identities and idempotent state
  transitions matter as much as model quality.
- **Unknown, uncertain, and not cooking are different product states.** UI
  actions must match what the system actually knows.
- **Security and cost boundaries belong inside the workflow.** They cannot be
  added after turning on autonomous processing.
- **A truthful demo is stronger than an accuracy claim the evidence cannot
  support.** Show the real action, full reasoning provenance, and honest limits.

## Current status

The authenticated MVP is hosted on Google Cloud. Durable capture, image storage,
event grouping, Gemini/ADK inference, feedback and revision history, household
knowledge, pattern questions, purchase ingestion, account data/export,
notifications, observability, and hard application controls are implemented.

Final physical-camera integration, long-running real-data evaluation, the
dedicated synthetic judge dataset, human UX acceptance, and release media remain
explicit gates. The architecture, deployment guide, submission copy, and
four-minute demo plan are source-controlled so the final entry can be reviewed
against what the deployed system genuinely proves.

Gemini FoodLog was created for the purposes of entering Google's 2026 All Things
Agentic Hackathon. The project targets **The Taskmaster** category: it removes a
messy recurring chore by operating asynchronously, maintaining state, asking
focused questions, learning from feedback, and taking the durable action of
keeping the journal up to date.

---

## Publication checklist — remove before publishing

- Oana approves the exact platform, title, byline, and final text.
- REL-011 approves every embedded screenshot or clip frame by frame.
- Replace no text with invented metrics, credentials, or private examples.
- Keep the explicit statement that this content was created for the hackathon.
- Publish as public, not unlisted.
- Verify the URL in a logged-out browser.
- Record the public URL in REL-013, then prepare the separately approved social
  post with `#AllThingsAgenticHackathon` exactly.
