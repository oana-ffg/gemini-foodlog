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

Three global lookup collections are permitted because their identifiers are needed
before the account is known:

- `identities/{firebase_uid}` maps a verified Firebase user to one account;
- `device_credentials/{credential_hash}` maps a hashed opaque device credential to
  one active camera and account;
- `inbound_mail_routes/{sha256_recipient}` maps a normalized opaque inbound address
  to one account address generation. Revoked route tombstones remain so an address
  can never be reassigned to another account.

Neither collection is readable by web or capture clients. Device credentials are
shown once at creation, stored only as hashes, revocable, and never reused between
cameras.

## Global documents

| Path | Purpose | Required fields and bounds |
| --- | --- | --- |
| `system/public_capacity` | Atomic 25-public-account admission counter | `active_account_count`, `account_limit`, `waitlist_open`; explicit internal/judge unlimited accounts never consume public slots; one low-write transaction document is acceptable at MVP scale. |
| `identities/{firebase_uid}` | Login-to-account lookup | `account_id`, `account_class` (`public` or explicitly configured `internal`), `email_normalized`, `email_verified`, `mailing_list_opt_in`, `status`; one document per Firebase UID. |
| `device_credentials/{sha256_token}` | Camera-token lookup | `account_id`, `camera_id`, `token_version`, `status` (`active` or `revoked`), `issued_at`, nullable `last_used_at`, nullable `expires_at`, nullable `revoked_at`; the raw token is returned only by the issuance response and is never stored. |
| `inbound_mail_routes/{sha256_recipient}` | Inbound-recipient lookup before the tenant is known | `account_id`, `address_id` (`current`), `status` (`active` or `revoked`), monotonic generation, `created_at`, and nullable `revoked_at`; the normalized address itself is not stored globally, and revoked tombstones are retained permanently to prevent reuse. |
| `waitlist/{sha256_firebase_uid}` | Capacity overflow and product-interest list | While active: verified `email_normalized`, `reason` (`capacity`), `mailing_list_opt_in` (`true`), `policy_version`, and timestamps. Withdrawal immediately sets `status=withdrawn`, clears the email, sets opt-in false, and retains only bounded withdrawal count/time evidence; the raw Firebase UID is never stored in the waitlist record. One document per verified identity is accepted or reactivated only while public capacity is full. |

## Account root and bounded counters

`accounts/{account_id}` stores only bounded account metadata: `owner_user_id`,
`status`, `timezone`, and consent/mailing-list summaries. Growing lists are always
subcollections.

`accounts/{account_id}/entitlements/current` stores the trial contract:
`entitlement_mode` (`trial` or `unlimited`), `accepted_image_count`, nullable
`trial_image_limit` (200 for public trials and null only for explicitly configured
internal/judge accounts). Capture acceptance updates this document transactionally
with the idempotency record. The 25-account/200-image prototype limits make the
single entitlement counter safe; sharded counters are deferred until demonstrated
write contention exists.

`system/model_spend` is the global Gemini hard-stop ledger. It stores currency
`DKK`, integer `limit_dkk_micros`, integer `reserved_dkk_micros`, reconciled
`actual_dkk_micros`, `reconciled_reservation_count`, and timestamps.
`system/model_spend/reservations/{reservation_id}` stores immutable account/event
scope, the reserved micro-DKK amount, status, and creation time. A Firestore
transaction reads the account, ledger, and idempotent reservation before atomically
creating the reservation and increasing the total. A lower persisted ceiling wins
over deployment configuration; a reservation that would exceed it performs no
write and fails before model invocation. The separate `system/model_spend_smoke`
ledger is an isolated, deliberately tiny deployed rejection proof and is never
consulted by production inference.

## Account-scoped collections

All IDs are random UUIDs unless a deterministic hash is explicitly named. Strings
are UTF-8, timestamps are server timestamps, and unbounded binary or model payloads
belong in private Cloud Storage rather than Firestore.

| Collection path below `accounts/{account_id}` | Purpose | Required fields and bounds |
| --- | --- | --- |
| `cameras/{camera_id}` | Browser or physical source | `name` <= 80 chars, `kind`, `status`, creation time, accepted-capture count, nullable last-capture time, nullable revocation time, and a hashed browser-instance identity for browser sources; no secret or raw device credential. Capture activity advances only after immutable image storage succeeds, and an exact idempotent retry does not increment it twice. |
| `inbound_mail_addresses/current` | Current private purchase-forwarding address | normalized `f-` plus 192-bit random token at the App Engine inbound-mail domain, `status` (`active` or `revoked`), monotonic generation, creation time, and nullable revocation time; generated server-side, contains no user/account identifier, and is returned only to the authenticated owner with `no-store`. Rotation atomically revokes the old global route and replaces this record; revoke leaves no active recipient. |
| `inbound_mail_usage/current` | Atomic inbound-mail cost and abuse ledger | persisted lower-wins message/byte and hourly request/byte ceilings, retained and pending message/byte counters, current rate-window counters/start, account ID, and timestamps. The gateway revalidates the active route and account in each admission/reservation transaction; exact raw-mail retries do not reserve retained capacity twice. |
| `capture_idempotency/{sha256_key}` | Exactly-once quota reservation | `capture_id`, `camera_id`, `content_sha256`, `content_type`, `state`; immutable after reconciliation except `state`. |
| `captures/{capture_id}` | One accepted image/frame | `camera_id`, nullable `segment_id`/`event_id`, `media_id`, `idempotency_hash`, `received_at`, `content_sha256`, `content_type`, server-derived original UTC-offset minutes, bounded versioned client `metadata` containing capture time, client, decoded dimensions, sequence/burst, and motion fields, plus `status` (`accepted`, `stored`, or `processed`). |
| `media/{media_id}` | Immutable private-object linkage | `capture_id`, server-derived `object_key`, generation, size, content type, SHA-256, `retention_class`; no public or signed URL. |
| `segments/{segment_id}` | One bounded capture burst inside an event | deterministic source identity, `event_id`, `camera_id`, first/last capture timestamps, and `capture_count`; frame IDs remain on capture documents rather than in a growing array. |
| `events/{event_id}` | Multi-frame kitchen activity | `camera_ids` <= 8, `status`, current subject revision, first/last capture timestamps, `capture_count`, nullable `meal_id`, grouping-policy version; frame IDs are queried from captures rather than accumulated in an array. |
| `model_usage/{reservation_id}` | Immutable per-invocation spend reconciliation | Reservation, account, and event scope; model/version, region, prompt version, purpose, retry/evaluation flags, success/failure outcome, integer token counts, integer USD nanos, conservative/actual DKK micros, bounded error code, and creation time. Raw prompts, responses, secrets, and chain-of-thought are forbidden. |
| `event_heads/{camera_id}` and `event_heads/account-affinity` | Transactional temporal-grouping pointers | latest event ID for one account camera or across the account plus update time; internal state only, never agent evidence. The account pointer lets temporally related evidence from different cameras join one event without any global or cross-tenant query. |
| `meals/{meal_id}` | Current materialized journal view | `event_id`, tentative `title`, confidence, status (`provisional`, `confirmed`, `corrected`, `contradicted`, or `not_cooking`), bounded components/observations/alternatives, rationale, revision number, `occurred_at`, and the originating camera timestamp's UTC-offset minutes. The journal list excludes `not_cooking`, while direct owner-scoped revision access remains available for audit and reclassification. |
| `meals/{meal_id}/revisions/{revision_id}` | Immutable inference/correction/disposition history | revision number, source, status, complete bounded inference snapshot, optional `feedback_id`, `base_revision_number`, and discriminated correction target (`meal`, `component`, `ingredient`, or `preparation_method`); unique revision number per meal. A `not_cooking` revision retains the prior inference, and explicit reclassification appends rather than overwrites. Targeted corrections require the current base revision and preserve every field outside their exact path. |
| `questions/{question_id}` | Agent-surfaced event or pattern question | `kind` (`event_clarification` or `pattern_hypothesis`), optional `meal_id`/`event_id`, prompt <= 500 chars, reason <= 2,000 chars, evidence refs <= 20, concrete choices for event questions, tentative claim for pattern questions, source revision, response linkage, and `status` (`open`, `answered`, or `superseded`). Rich longitudinal hypotheses additionally store the normalized claim, ordered observation window, dated supporting examples and counterexamples with original UTC-offset minutes, explicit uncertainty, prompt version, deterministic evidence hash/topic key, and optional rejected predecessor. Generic “what meal were you cooking?” questions are not permitted: event corrections belong on the meal card. |
| `question_responses/{sha256_key}` | Immutable user response to an agent question | question ID, `kind` (`confirm`, `correct`, or `reject`), optional exact correction <= 500 chars and explanation <= 4,000 chars, optional derived meal-feedback ID, and `idempotency_hash`; the raw response remains separate from later meal or knowledge derivations. |
| `pattern_hypothesis_topics/{topic_hash}` | Bounded latest-hypothesis pointer | deterministic structured-claim topic hash, latest question ID, account ID, schema version, and update time. It avoids scans when suppressing duplicate or insufficiently changed rejected hypotheses; it contains no raw meal or user text. |
| `feedback/{feedback_id}` | Immutable user confirmation/correction/discard | `meal_id`, optional `question_id`, `kind` (`confirm`, `correct`, or `not_cooking`), legacy whole-meal actual value, optional exact correction target and base revision, exact explanation/reason <= 2,000 chars, optional explicit learning disposition (`reusable` or `insufficient_information`), and `idempotency_hash`; never overwrites the original inference. Not-cooking forbids correction/learning fields, may retain an optional reason, and requires an explicit replacement correction before the meal can return to the journal. Reusable disposition requires both a replacement and explanation; the backend does not infer it from prose. |
| `knowledge/{knowledge_id}` | Current household-wiki page projection | deterministic account/topic identity, normalized topic key, human-readable title and statement, optional normalized claim dimension/value/exact applicability conditions for legacy compatibility, lifecycle (`inferred`, `reinforced`, `confirmed`, `contradicted`, or `retired`), belief strength (`weak`, `moderate`, or `strong`), and current revision ID/number. |
| `knowledge/{knowledge_id}/revisions/{revision_id}` | Immutable household-wiki history | complete title/statement/structured-claim/lifecycle/strength snapshot, source (`agent_inference`, `user_feedback`, `user_statement`, or `question_response`), bounded evidence references with support/contradiction/context roles, human-readable reason, base revision, previous revision ID, and creation time. Every revision after the first must cite its immediate predecessor as provenance. New agent-proposed revisions carry a normalized claim. Direct literal user statements may leave the claim null rather than invent a semantic rewrite; legacy claim-less revisions remain readable for the same field-level compatibility. |
| `knowledge_revision_requests/{sha256_key}` | Exactly-once wiki revision identity | page/revision IDs, canonical request hash, and creation time; contains no raw idempotency key. |
| `user_context_notes/{sha256_account_key}` | Immutable proactive household context and stable-teaching source evidence | exact trimmed user wording <= 4,000 chars, author identity, optional offset-aware `valid_from`/`valid_until` window, status (`active` or `retired`), creation/retirement timestamps, and canonical request hash; contains no raw idempotency key. Active agent reads exclude future, expired, and retired notes, while owner history reads preserve them. A deliberate stable teaching or correction creates a no-window note, links it from the confirmed wiki revision, and immediately retires it so the same statement is not duplicated in temporary agent context. |
| `purchases/{purchase_id}` | One retailer purchase lifecycle | merchant, bounded `revision_count`, latest confirmation/final document IDs, and creation/update timestamps; order/invoice aliases and source documents remain separate so the root never grows arrays. |
| `purchase_identities/{sha256_merchant_kind_reference}` | Exact business-identity alias | purchase ID, merchant, kind (`order` or `invoice`), reference hash, creation time; aliases are account-scoped, contain no plaintext reference, and conflicting aliases never trigger an automatic merge. |
| `purchase_documents/{raw_mail_id}` | One immutable normalized source-document identity | purchase ID, raw-mail/content hashes, merchant, document kind, revision number, and optional normalized retailer-labelled order/invoice references; an exact transport retry returns this document while changed interpretation conflicts. |
| `purchase_normalizations/{raw_mail_id}` | Immutable parser result identity | purchase/source revision, document kind, parser version, canonical normalization hash, item/charge counts, optional included VAT, and creation time; exact retries return the existing result while changed normalized output conflicts. |
| `purchase_items/{sha256_document_ordinal}` | Immutable ordered or delivered product evidence | purchase/source document and revision, contiguous source ordinal, exact and normalized names, ordered/delivered disposition, quantity, optional category/unit/discount, unit price, and line total in DKK øre. |
| `purchase_charges/{sha256_document_kind}` | Immutable source-document charge | purchase/source document, fixed charge kind, exact bounded description, and amount in DKK øre. |
| `purchase_reconciliations/{purchase_id}` | Current deterministic order-versus-delivery projection | latest normalized confirmation/final source IDs, canonical reconciliation hash, exact-name/quantity dispositions, source item IDs, unresolved count, and an explicit flag when removed plus added lines may be substitutions but the retailer evidence does not prove the pairing. |
| `raw_mail/{mail_id}` | MIME transport metadata and immutable object link | recipient, nullable sender <= 500 chars, normalized sender address, nullable subject <= 500 chars, nullable normalized message-ID hash, fixed `trust_class` (`untrusted_external`), bounded MIME part/attachment counts and accepted content-type list, content SHA-256, byte size, server-derived `object_key`, transport status (`reserved`, `stored`, or `published`), bounded publish attempt/provider IDs, and timestamps; MIME bytes stay in GCS and parsing adds separate normalized purchase evidence. The worker may consume `stored` or `published` after a genuine Pub/Sub event because publishing necessarily follows the durable GCS write and `stored` transition; it rejects `reserved`, missing, cross-account, or hash-mismatched records. |
| `raw_mail_authentication/{mail_id}` | Immutable sender-authentication verdict | account/raw-mail/content-hash binding, `aligned_dkim_pass` or `untrusted` outcome, verifier version and timestamp, and—for a pass—the aligned Nemlig signer plus signed-header evidence. The private worker derives this record by cryptographically checking the exact retained bytes; MIME `Authentication-Results` and `ARC-Authentication-Results` text is never a verdict. Purchase attachment requires the matching passing record in the same transaction. |
| `raw_mail_processing/{mail_id}` | Immutable terminal mail-processing disposition | account/raw-mail/content-hash binding, rejected phase and bounded failure code, purchase-document kind, parser version, and timestamp. Deterministic authenticated-PDF format/resource rejections are recorded before Pub/Sub acknowledgement so exact retries are no-ops; infrastructure failures do not create this record and remain retryable. |
| `traces/{trace_id}` | Agent-run index | deterministic trace, root-trace, optional parent-trace, event, and model-reservation IDs; model/provider/prompt versions, purpose, retry/evaluation lineage, outcome/error code, started/completed timestamps, latency, token/cost counters, compressed byte size, and immutable GCS `object_key` plus SHA-256. Full application-visible trace content stays in GCS; invocation keys are hashed and secrets or hidden reasoning are forbidden. |
| `audit_events/{sha256_action_subject}` | Immutable bounded operation evidence | schema version, deterministic ID, account ID, action, actor kind, source, bounded subject kind/ID, and creation time. Records contain no token, credential, request body, prompt, response, image bytes, or free-form user/model text. Repeated delivery of the same action for the same subject returns the original event instead of creating read-amplified duplicates. |
| `jobs/{job_id}` | Durable asynchronous work state | `kind` (`capture_grouping`, `event_inference`, or `account_export`), subject ID and revision, status, attempt count, `available_at`, lease token/owner/expiry, last error code/message <= 2,000 chars; payload <= 20 scalar/reference keys. |
| `exports/{export_id}` | User data-export state | Deterministic opaque export ID, requesting owner, matching durable-job ID, status, immutable request/snapshot boundary, and later completed/expiry timestamps, temporary GCS object key, byte size, and SHA-256; no raw idempotency key, and the object expires after one day. |
| `export_control/current` | Transactional export admission fence | Account ID, at most one active export ID, and last accepted request time. It enforces one active job plus the configured request cooldown without scanning account exports. |
| `consents/{sha256_actor_email_kind_policy_decision}` | Immutable consent change | `kind` (`launch_mail`), `granted`, `policy_version`, `actor_user_id`, `email_normalized`, timestamp; identical retries deduplicate while a changed decision or verified email appends a new event. The owning identity also holds the current decision and its dedicated update time so withdrawing and later opting in again cannot leave stale state. |
| `outbox/{message_id}` | Transactional notification/email intent | `kind`, status, dedupe hash, available timestamp, attempt count, recipient identity reference, payload <= 20 scalar/reference keys; no provider secret. |

## Write invariants

1. Object keys are derived from authenticated account, camera, capture, and content
   identifiers; request bodies cannot choose them. Every object-store call also
   carries that authenticated account as a separate argument, and the adapter
   rejects a key outside its exact `accounts/{account_id}/` prefix—or containing
   empty, dot, dot-dot, or backslash segments—before any Cloud Storage operation.
   Feature adapters may narrow this further; raw mail, for example, requires the
   exact `accounts/{account_id}/raw-mail/{mail_id}.eml` path.
2. A capture transaction reads the entitlement and idempotency record, then either
   returns the existing matching capture or reserves one quota unit and one capture.
3. If object upload fails, reconciliation returns the reservation to a retryable
   state; a retry never consumes a second quota unit.
4. Immutable records—media links, feedback, meal revisions, consent events, raw-mail
   identity, trace identity, and audit events—are append-only. Corrections update a
   materialized view and append evidence. An audit ID deterministically binds its
   action, actor kind, source, subject, and owning account; an exact retry returns the
   original record, while conflicting reuse fails closed.
5. A targeted meal correction names its immutable base revision and exact structural
   path. The transaction rejects stale bases or invalid paths, copies every unrelated
   component and evidence field forward, appends one revision, and only then updates
   the materialized meal view.
6. A household-wiki write atomically verifies its expected base revision, allowed
   lifecycle transition, and direct predecessor evidence before appending an immutable
   revision and updating the page projection. A retired page cannot silently reactivate.
7. Every reference is verified to remain inside the same account before commit.
8. Growing arrays are forbidden. Evidence lists have explicit caps; full evidence,
   frames, line items, and revisions use subcollections.
9. Firestore documents remain comfortably below its 1 MiB limit; application writes
   reject the tighter bounds above before serialization.
10. Inbound mail reserves its deterministic account-scoped transport identity before
   object upload, moves through stored/published states, and retries unfinished
   publication. Admission first charges a bounded request/byte rate window, then a
   second transaction revalidates the active address generation and reserves retained
   message/byte capacity. A reused Message-ID with different bytes falls back to a
   content-qualified identity so conflicting evidence is preserved rather than
   overwritten or dropped. Exact retries do not reserve retained capacity twice.
11. Inbound MIME bytes are always `untrusted_external`. The gateway validates bounded
   structure and supported passive content before storage, publishes no message body
   or attachment content, and never promotes email text into executable agent
   instructions.
12. Purchase identity uses only explicit account/merchant/order/invoice aliases. A
    raw-message retry is one immutable source document; later documents sharing an
    exact alias append revisions. Identifier-free or merely similar documents remain
    separate, and a message bridging aliases already owned by different purchases
    fails closed instead of merging history.

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
