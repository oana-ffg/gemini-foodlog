# Judge account and dataset runbook

This runbook prepares one dedicated production judge account without copying any
existing account, kitchen image, mail, purchase, trace, or household note.

## Safety boundary

- Run only after Oana approves the exact identity, secret-store mutations, and
  bounded model usage.
- Keep the email and password in environment variables populated from the local
  secret store. Never put either value in source, command arguments, screenshots,
  terminal transcripts, or the submission copy.
- The identity must use the reserved `.invalid` domain and a strong password of
  at least 20 characters.
- The account is an internal unlimited account, so it does not consume one of the
  25 public slots. The global DKK 400 model-spend ceiling remains unchanged.
- The script permits no more than 25 account-visible model traces. It stops
  before starting another fresh inference if a possible repair call could exceed
  that ceiling. The account already contains 17 immutable traces, including the
  retained retry-safety incident evidence. Oana authorized up to DKK 200 of new
  testing spend on 28 Aug 2026. The first four recovery traces are now immutable;
  this final bounded window permits at most four more traces for one fresh cat
  control and, only if still useful, one later ambiguity control.
- The script creates no purchase evidence. A fixture message cannot honestly
  stand in for aligned DKIM from Nemlig.
- The six dated pattern meals are plainly labelled in their immutable evidence as
  synthetic, no-model history. Their only purpose is to demonstrate the
  longitudinal question UX.
- The process is create-only and idempotent. It does not delete an identity or
  account and uses deterministic capture identities for resumable synthetic
  history.

## Frozen input

[`judge-demo-dataset.v4.json`](../tests/fixtures/judge-demo-dataset.v4.json)
is the current create-only recovery revision and hash-locks every image. The
committed v1 through v3 manifests remain immutable evidence of the earlier runs;
v4 keeps the degraded fixture but gives the post-semantic-guard calibration run
an explicit fourth-attempt client version and idempotency key. Existing captures
therefore remain auditable while the retry cannot be mistaken for a duplicate.
It defines:

- a time-bounded synthetic note that chicken is available;
- a real red-meat inference, correction, reusable learning, and later real
  follow-up that must cite that exact learning revision;
- a real ambiguous-meat inference that must remain uncertain, cite the note, and
  ask a focused candidate question;
- a real cat negative control that must classify as likely non-cooking before its
  immutable discard feedback;
- six explicitly synthetic, no-model Thursday meals with one chicken
  counterexample, yielding one open steak-pattern question.

If the 25-trace ceiling is reached, lower-priority remaining real scenarios are
left absent rather than exceeding the approved spend. The final inventory reports
how many were completed and skipped without printing generated resource IDs.

## Execution

From `services/backend`, populate the two credential environment variables from
the approved local secret entries without echoing them, then run:

```bash
uv run python -m scripts.prepare_judge_dataset \
  --api-url https://foodlog-api-sptvo5nsga-ew.a.run.app \
  --firebase-api-key "$FOODLOG_FIREBASE_API_KEY" \
  --origin https://gemini-foodlog-2026.web.app \
  --project gemini-foodlog-2026 \
  --bucket gemini-foodlog-2026-media-163029863855 \
  --notification-topic projects/gemini-foodlog-2026/topics/foodlog-notification-events \
  --manifest ../../tests/fixtures/judge-demo-dataset.v4.json \
  --fixture-root ../../tests/fixtures \
  --create-approved-identity \
  --confirm-production-write
```

The browser Firebase API key is public configuration, but still load the live
configured value rather than documenting it here. The two confirmation flags are
deliberately explicit: without them the script cannot create the approved
identity or write the production dataset.

For bounded recovery after an earlier scenario has persisted but failed its
acceptance gate, append `--only-scenario <manifest-key>`. This keeps the
scenario's original sequence number and upload identity; it does not rewrite or
silently accept the failed result.

## Required successful evidence

The sanitized summary must report:

- verified identity and unlimited entitlement;
- published account-created notification;
- completed and call-cap-skipped real scenario counts;
- model trace count no greater than 25;
- six synthetic pattern meals and exactly one open Thursday steak question;
- final journal, discarded-history, and active-note counts;
- confirmation that credentials and generated resource IDs were omitted.

After seeding, perform the judge workflow in
[`devpost-submission.md`](devpost-submission.md) using the hosted UI. Credentials
belong only in Devpost's private testing-instructions field or an equally private
judge channel; they never belong in the repository or public video.
