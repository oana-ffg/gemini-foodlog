# Firestore schema v1

This document is the canonical durable-data contract for the Gemini FoodLog MVP.
It defines paths and invariants, not Python storage implementation details. Every
stored document has `schema_version: 1`, `created_at`, and `updated_at` unless the
record is immutable and therefore has only `created_at`.

## Tenant boundary

The account is the tenant. All household data lives below
`accounts/{account_id}`. Repository methods start from an authenticated account and
construct paths server-side; callers never submit a trusted `account_id` or object
path. The API never runs collection-group queries for user-facing reads.

Two global lookup collections are permitted because their identifiers are needed
before the account is known:

- `identities/{firebase_uid}` maps a verified Firebase user to one account;
- `device_credentials/{credential_hash}` maps a hashed opaque device credential to
  one active camera and account.

Neither collection is readable by web or capture clients. Device credentials are
shown once at creation, stored only as hashes, revocable, and never reused between
cameras.

## Global documents

| Path | Purpose | Required fields and bounds |
| --- | --- | --- |
| `system/public_capacity` | Atomic 25-account admission counter | `active_account_count`, `account_limit`, `waitlist_open`; one low-write transaction document is acceptable at MVP scale. |
| `identities/{firebase_uid}` | Login-to-account lookup | `account_id`, `email_normalized`, `email_verified`, `mailing_list_opt_in`, `status`; one document per Firebase UID. |
| `device_credentials/{sha256_token}` | Camera-token lookup | `account_id`, `camera_id`, `token_version`, `status`, `last_used_at`, `expires_at`; never stores the token. |
| `waitlist/{sha256_email}` | Capacity overflow and product-interest list | `email_normalized`, `firebase_uid`, `reason`, `mailing_list_opt_in`; one document per normalized email. |

## Account root and bounded counters

`accounts/{account_id}` stores only bounded account metadata: `owner_user_id`,
`status`, `timezone`, and consent/mailing-list summaries. Growing lists are always
subcollections.

`accounts/{account_id}/entitlements/current` stores the trial contract:
`accepted_image_count`, `trial_image_limit` (initially 200), `model_spend_reserved`,
and `model_spend_limit`. Capture acceptance updates this document transactionally
with the idempotency record. The 25-account/200-image prototype limits make the
single entitlement counter safe; sharded counters are deferred until demonstrated
write contention exists.

## Account-scoped collections

All IDs are random UUIDs unless a deterministic hash is explicitly named. Strings
are UTF-8, timestamps are server timestamps, and unbounded binary or model payloads
belong in private Cloud Storage rather than Firestore.

| Collection path below `accounts/{account_id}` | Purpose | Required fields and bounds |
| --- | --- | --- |
| `cameras/{camera_id}` | Browser, simulator, or physical source | `name` <= 80 chars, `kind`, `status`, `client_version` <= 80 chars, `last_seen_at`; no secret. |
| `capture_idempotency/{sha256_key}` | Exactly-once quota reservation | `capture_id`, `camera_id`, `content_sha256`, `content_type`, `state`; immutable after reconciliation except `state`. |
| `captures/{capture_id}` | One accepted image/frame | `camera_id`, `event_id`, `media_id`, `idempotency_hash`, `captured_at`, `received_at`, `content_sha256`, `content_type`, dimensions, byte size, sequence metadata, motion metadata, `status`; motion metadata <= 20 scalar keys. |
| `media/{media_id}` | Immutable private-object linkage | `capture_id`, server-derived `object_key`, generation, size, content type, SHA-256, `retention_class`; no public or signed URL. |
| `events/{event_id}` | Multi-frame kitchen activity | `camera_ids` <= 8, `status`, first/last capture timestamps, `capture_count`, `meal_id`, grouping-policy version; frame IDs are queried from captures rather than accumulated in an array. |
| `meals/{meal_id}` | Current materialized journal view | `event_id`, tentative `title`, `classification` (`guess`, `unknown`, or `not_cooking`), confidence, review state, bounded components/observations/alternatives, rationale <= 8,000 chars, `current_revision`, `occurred_at`; a truly unknown record cannot be confirmed as correct. |
| `meals/{meal_id}/revisions/{revision_id}` | Immutable inference/correction history | revision number, source, status, complete bounded inference snapshot, `feedback_id`, `trace_id`; unique revision number per meal. |
| `questions/{question_id}` | Agent-surfaced event or pattern question | `kind` (`event_clarification` or `pattern_hypothesis`), optional `meal_id`, prompt <= 500 chars, reason <= 2,000 chars, evidence refs <= 20, tentative claim, `status`, answer <= 2,000 chars. Generic “what meal were you cooking?” questions are not permitted: event corrections belong on the meal card. |
| `feedback/{feedback_id}` | Immutable user confirmation/correction/discard | optional `meal_id`/`question_id`, `kind`, actual value <= 500 chars, explanation <= 4,000 chars, `idempotency_hash`; never overwrites the original inference. |
| `knowledge/{knowledge_id}` | Revisable household facts and hypotheses | statement <= 2,000 chars, `kind`, confidence, status, evidence refs <= 50, supersedes ID, learned-from feedback/question IDs; conflicts create revisions, not silent replacement. |
| `purchases/{purchase_id}` | Normalized invoice/order | merchant, order/reference hash, purchased/delivery timestamps, currency, bounded totals, `raw_mail_id`; line items are separate documents. |
| `purchases/{purchase_id}/items/{item_id}` | One purchased product | normalized name <= 500 chars, quantity/unit, amount, category, source text <= 1,000 chars. |
| `raw_mail/{mail_id}` | MIME metadata and immutable object link | sender/recipient, message-ID hash, subject <= 500 chars, received timestamp, `object_key`, content SHA-256, parser status; MIME bytes stay in GCS. |
| `traces/{trace_id}` | Agent-run index | event/job IDs, model and prompt versions, status, started/completed timestamps, token/cost counters, immutable GCS `object_key` and SHA-256; full trace stays in GCS. |
| `jobs/{job_id}` | Durable asynchronous work state | `kind`, subject ID, status, attempt count, `available_at`, lease owner/expiry, last error code/message <= 2,000 chars; payload <= 20 scalar/reference keys. |
| `exports/{export_id}` | User data-export state | status, requested/completed/expiry timestamps, temporary GCS object key, byte size, SHA-256; object expires after one day. |
| `consents/{consent_id}` | Immutable consent change | consent kind, granted boolean, policy version, actor identity, timestamp. |
| `outbox/{message_id}` | Transactional notification/email intent | `kind`, status, dedupe hash, available timestamp, attempt count, recipient identity reference, payload <= 20 scalar/reference keys; no provider secret. |

## Write invariants

1. Object keys are derived from authenticated account, camera, capture, and content
   identifiers; request bodies cannot choose them.
2. A capture transaction reads the entitlement and idempotency record, then either
   returns the existing matching capture or reserves one quota unit and one capture.
3. If object upload fails, reconciliation returns the reservation to a retryable
   state; a retry never consumes a second quota unit.
4. Immutable records—media links, feedback, meal revisions, consent events, raw-mail
   identity, and trace identity—are append-only. Corrections update a materialized
   view and append evidence.
5. Every reference is verified to remain inside the same account before commit.
6. Growing arrays are forbidden. Evidence lists have explicit caps; full evidence,
   frames, line items, and revisions use subcollections.
7. Firestore documents remain comfortably below its 1 MiB limit; application writes
   reject the tighter bounds above before serialization.

## Query and index contract

The backend uses account-subcollection queries only. The checked-in
`infra/firestore/firestore.indexes.json` declares composite indexes for the query
shapes that combine state with time ordering. Ordinary ID lookups and one-field
ordering use Firestore's automatic single-field indexes.

- cameras by `status`, then recent activity;
- captures by camera and capture time, or processing status and receipt time;
- events by status and last activity for grouping/closure;
- meals by review state and meal time;
- questions by status and creation time;
- knowledge by status and update time;
- jobs/outbox by status and next eligible attempt.

Large explanatory fields, payload maps, hashes used only for equality-by-ID, and GCS
object keys are exempted from single-field indexing to reduce index storage and avoid
index-entry limits. New query shapes require a reviewed index-file change; production
code must not depend on links from Firestore's runtime “create index” error page.
