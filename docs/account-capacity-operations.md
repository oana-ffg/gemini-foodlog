# Public account capacity operations

FoodLog admits at most 25 public trial accounts. A verified Firebase identity is
necessary but is not a uniqueness proof: one person can create several verified
mailboxes or aliases. The MVP therefore prevents permanent slot exhaustion with a
reversible operator workflow. It does not claim to prevent temporary exhaustion
before abuse is reviewed.

## Safety boundary

- Admission atomically stores the verified normalized email evidence with the
  identity, account, entitlement, notification outbox, waitlist fulfilment, and
  public counter.
- Reclamation never deletes an account or its data. It atomically marks the account
  and identity `capacity_reclaimed`, decrements the active-public counter, opens the
  waitlist when appropriate, and writes immutable lifecycle and audit evidence.
- Restoring the same account is a separate audited operation. It fails while all
  public slots are occupied and increments the counter exactly once.
- Internal or judge accounts cannot be reclaimed through this command.
- Reclaimed identities cannot use owner APIs, authenticate cameras, finish a
  reserved capture, claim new worker jobs, or complete/retry an existing job.
- A Gemini call that reserved spend while the account was active may still write
  its immutable usage settlement and application-visible trace after reclamation.
  This is accounting evidence for cost already incurred, not permission to publish
  an inference, meal, question, export, or other user-domain result.
- Each mutation is keyed by an operator-supplied UUID. Retrying the exact operation
  is idempotent; changing its reason fails closed.

## Required investigation

Use Firebase Authentication and the account-scoped diagnostics to establish the
exact UID and account. `missing_firebase_identity` is only appropriate after an
exact Firebase lookup proves the UID no longer exists. Never infer abuse from an
email domain, alias shape, inactivity, image contents, or a full capacity counter.

Before the first production use after this schema change, reconcile every active
public identity against its account, trial entitlement, Firebase UID, and the
global capacity counter. Backfill `admission_email_normalized` and
`admission_email_verified=true` only when the exact Firebase UID currently has a
verified normalized email. Duplicate current emails, malformed admission evidence,
missing account/entitlement records, counter drift, and missing Firebase identities
are report-and-stop conditions. Missing identities are reclamation candidates,
never automatic reclamations. A later verified-email change does not overwrite the
original admission evidence or change ownership of the stable Firebase UID.

The source-controlled reconciler is dry-run-only unless the exact verified-email
backfill flag is supplied:

```bash
services/backend/.venv/bin/python -m foodlog_backend.account_capacity_reconcile_main \
  --project-id gemini-foodlog-2026
```

Review a clean report, then repeat with `--apply-email-backfill`. The command stops
instead of writing when it finds any binding, counter, duplicate-email, changed-email,
unverified-email, or missing-Firebase-identity finding, and reruns the complete
reconciliation after backfill.

## Dry run and apply

Set `FOODLOG_ACCOUNT_ID` from the exact audited Firestore record and generate one
UUID for this logical operation. Reuse that UUID only when retrying the same action.

```bash
export FOODLOG_ACCOUNT_ID
export FOODLOG_CAPACITY_OPERATION_ID

services/backend/.venv/bin/python -m foodlog_backend.account_capacity_main \
  --project-id gemini-foodlog-2026 \
  --account-id "$FOODLOG_ACCOUNT_ID" \
  --action reclaim \
  --reason confirmed_sybil_abuse \
  --operation-id "$FOODLOG_CAPACITY_OPERATION_ID"
```

The default command is read-only and prints the current account/identity status and
counter. Review it, then repeat the exact command with `--apply`. Use
`missing_firebase_identity` only for the exact missing-UID case above.

To reverse a decision, generate a new operation UUID and run `--action restore
--reason operator_reversal`; review the dry run, then repeat with `--apply`.

## Verification

After an applied operation, the command reads back the account, identity, capacity
counter, and immutable operation record before reporting success. Also verify:

1. `system/public_capacity.active_account_count` matches the counted active public
   identities and `waitlist_open` matches whether the limit is full.
2. The account and identity have the same expected status and operation UUID.
3. `accounts/{account_id}/capacity_operations` contains the operation.
4. `accounts/{account_id}/audit_events` contains the matching security-review event.
5. A reclaimed Firebase session and camera credential are rejected, a capture
   reserved before reclamation cannot become stored, and its worker job cannot be
   claimed.
6. A legitimate waitlisted identity can claim the released slot and its waitlist
   record becomes `fulfilled` without retaining its email or mailing consent.
