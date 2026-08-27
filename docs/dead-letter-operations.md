# Dead-letter inspection and replay

FoodLog keeps one retained inspection subscription per event stream. The local
operator command uses Application Default Credentials, so Google IAM and Data
Access logs identify the person performing each Pub/Sub read or acknowledgement.
Every valid message also creates account-scoped application audit events.

The command accepts only Terraform-owned subscription and topic names derived
from the exact project and stream. It cannot choose an arbitrary queue or topic.
It validates the stream schema, source project, source subscription, source
delivery count, account identity, and subject identity before returning metadata
or changing queue state. It never returns acknowledgement IDs or unvalidated
payload fields.

## Inspect without removing work

Run from `services/backend`:

```sh
uv run python -m foodlog_backend.dead_letter_operations_main \
  --project-id gemini-foodlog-2026 \
  --stream image \
  --purpose incident_triage \
  inspect --max-messages 10
```

Inspection leases at most ten messages, validates and audits them, returns only
bounded metadata, then sets their acknowledgement deadlines to zero. It never
acknowledges or republishes them. An invalid message fails closed and is also
released immediately.

## Replay one exact message

Replay requires the inspected message ID in both `--message-id` and
`--confirm-message-id`. Only a successful publish of the validated canonical
event to the Terraform-owned source topic permits the source dead-letter message
to be acknowledged. Run the module with `replay --help` for the exact syntax;
the operator must supply identifiers from the current inspection rather than a
copied example.

The request and successful publish are separate immutable audit events. If
validation, auditing, or publication fails, the dead-letter message is released
and remains available. If publication succeeds but acknowledgement is retried,
the workers' durable job and immutable projection contracts make the repeated
event a no-op rather than a second meal, purchase, or notification.

The image topic fans out to grouping and inference. Before replaying an old image
message, inspect its `source_subscription` and durable event state; replaying a
grouping failure can also wake a pending inference job.

## Remove already-resolved image residue

An old image failure can remain retained even after its exact durable job was
recovered by an earlier manual replay. Do not republish it. Use the narrower
`acknowledge-resolved-image` command with the inspected ID repeated in
`--message-id` and `--confirm-message-id`, plus its exact capture ID in
`--confirm-capture-id`. Run that subcommand with `--help` for syntax and always
copy the identifiers from the current inspection.

This path validates that the dead-letter source is either the grouping or
inference subscription, derives the corresponding deterministic job ID, and
requires that exact job to be `completed`. Pending, leased, missing, mismatched,
or cross-project work is released and cannot be acknowledged. The requested and
completed acknowledgement are recorded separately.

## Failure semantics

- Empty inspections are successful and return an empty list.
- The operator must rerun inspection if the exact replay target is not in the
  bounded pull.
- There is no bulk acknowledgement, purge, seek, delete, payload dump, tenant
  impersonation, or arbitrary destination mode.
- Mail and notification messages support schema-checked inspection and explicit
  replay. Completed-job acknowledgement is deliberately image-only because only
  image processing currently has a durable job whose completed state can prove
  the retained message is obsolete.
