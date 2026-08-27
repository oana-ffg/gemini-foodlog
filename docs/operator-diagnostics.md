# FoodLog operator diagnostics

This is the narrow prototype support path for investigating one known activity
event. It is deliberately a local command using the operator's existing Google
Application Default Credentials. FoodLog does not expose an admin API, service
account key, user-impersonation path, or bulk evidence browser.

## Authorization and audit boundary

Every run requires an exact account ID, exact event ID, and one of four purposes:
`incident_triage`, `support`, `security_review`, or
`development_verification`. The command appends a unique immutable
`operator.diagnostic_read` audit event before it attempts to read the selected
event. Retrying the same explicit session ID is idempotent; a new run gets a new
session ID and therefore a new audit record. Google Cloud Audit Logs remain the
authoritative record of the Google principal whose ADC performed the Firestore,
Storage, and Logging reads.

The command returns only:

- activity-event, capture, and durable-job state from the selected account/event;
- media-object size, content type, generation, CRC32C, and update time;
- trace index metadata plus schema, identity, hash, redaction, and tool-event counts;
- structured operational logs already constrained by the application's allowlist.

It never returns image bytes, raw MIME, trace prompts or responses, user/model free
text, credentials, request or response bodies, or hidden reasoning. Unexpected log
fields, trace identity mismatches, object metadata mismatches, incomplete event
evidence, and more than 200 captures, 25 traces, or 100 log records fail closed.
There is no replay, queue pull, queue acknowledgement, deletion, or write mode.

## Verified production target

From `services/backend`, inspect the retained telemetry-verification event with:

```shell
uv run python -m foodlog_backend.operator_diagnostics_main \
  --project-id gemini-foodlog-2026 \
  --media-bucket gemini-foodlog-2026-media-163029863855 \
  --trace-bucket gemini-foodlog-2026-traces-163029863855 \
  --account-id 04aa46b9-697f-460e-99c0-09c2b21cdfe9 \
  --event-id 1df89f15-acf1-4b58-b0d3-0b417ff89fe9 \
  --purpose development_verification
```

Run this only from Oana's authenticated workstation. Do not redirect the JSON to a
shared file or paste it into public issue trackers. A successful run must show the
requested account/event IDs, a nonempty audit event ID, matching Firestore and GCS
metadata, verified trace redaction/integrity, and only allowlisted logs. The command
must then be corroborated by reading the new immutable audit record and the Google
Cloud audit trail rather than treating terminal output alone as proof.
