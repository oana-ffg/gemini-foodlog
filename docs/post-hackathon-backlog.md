# Post-hackathon backlog

- **Status:** Deferred work that is intentionally outside the hackathon MVP
- **Last updated:** 2026-08-25
- **MVP decisions:** [mvp-architecture.md](mvp-architecture.md)

This is the canonical list of product and architecture changes to reconsider after the hackathon. Adding an item here does not commit us to a particular solution or priority.

## Data lifecycle and privacy

### Replace indefinite prototype retention

Choose and implement fixed retention periods for:

- camera images and other raw media;
- raw forwarded emails and attachments;
- full AI traces;
- generated and intermediate agent artifacts.

Define what derived meal evidence may survive source expiry, whether users can shorten or extend the defaults, and how the UI communicates missing or expired evidence.

### User-controlled deletion

Let a user delete individual images, events, emails, traces, or their entire account. Define how deletion propagates through Cloud Storage, Firestore metadata, derived meal records, knowledge provenance, exports, caches, and backups without leaving broken or misleading evidence.

### Review operator and agent access

Revoke any dedicated Codex or Antigravity credential created during the hackathon. Replace direct prototype access with a product-grade support mechanism offering narrower account selection, time limits, purpose records, auditing, and any user-facing access controls or notifications required for a full product.

## Product expansion

### Symptom investigation

Add symptom entry or integrations and food/symptom association analysis. Keep the distinction between an observed correlation and a medical conclusion explicit.

### Individual household histories

Consider attributing portions or meals to individual household members instead of treating preparation as one shared household meal.

### Shared household access

Add invitations, multiple authenticated members per household account, member removal, and explicit roles. Decide whether every member can view images, emails, AI traces, exports, and household knowledge, and define what happens to their contributions when they leave.

### Additional capture clients

Evaluate phone and other camera clients after the physical microcontroller and webcam simulator have established a stable shared protocol.

### Full-product onboarding and capacity

Replace the 25-account hackathon gate and 200-image trial with product-appropriate capacity, abuse controls, entitlements, and—only if needed—billing. Select an outbound email provider before sending the consented full-product availability notice to the waitlist.

## Maintenance

### Reconsider Firebase App Check before wider public access

Evaluate App Check or an equivalent browser-abuse control together with a hard,
verified spend boundary. reCAPTCHA Enterprise assessments can be consumed by
anonymous visitors before account admission, its free 10,000 monthly assessments
are shared across the organization, and billing-enabled projects automatically enter
the paid tier after that allowance. Do not enable enforcement merely because the API
is available; first prove that unexpected assessment traffic cannot create
out-of-pocket spend.

### Migrate deprecated test and agent-framework interfaces

Replace ADK's deprecated `BaseAgentConfig` path before its next major removal, and
migrate FastAPI/Starlette integration tests from the deprecated `httpx` TestClient
compatibility path to `httpx2` once the dependency set supports it. Both are warning-only
with the current locked dependencies; the full MVP backend suite still passes.
