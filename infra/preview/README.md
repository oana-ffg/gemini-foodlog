# Private Cloud Run preview

This preview exists only to smoke-test a committed backend revision on Google Cloud before the durable production adapters are ready.

## Security and cost boundary

- Cloud Run rejects unauthenticated callers through IAM.
- The application independently requires a generated preview secret stored in Secret Manager.
- Gemini and every other external model call remain disabled; inference only recognizes immutable local fixture hashes.
- The service has zero minimum instances and one maximum instance.
- Account, image, meal, feedback, and question state is in memory and may disappear on any cold start or revision change.
- Production configuration still fails closed until the private GCS, Firestore, Firebase Authentication, and worker adapters exist.

The preview is not a public trial, production deployment, or durability claim. Its purpose is an authenticated live smoke test and Cloud Run reporting verification.

## Live resources

Verified on 2026-08-25:

- Google Cloud project: `gemini-foodlog-2026`
- Region: `europe-west1`
- Service: `foodlog-preview-api`
- URL: `https://foodlog-preview-api-163029863855.europe-west1.run.app`
- Ready revision: `foodlog-preview-api-00002-clf`, serving 100% of traffic
- Runtime identity: `foodlog-preview-runner@gemini-foodlog-2026.iam.gserviceaccount.com`
- Scaling: automatic, zero minimum instances, one service-level and revision-level maximum instance
- Runtime limits: one vCPU, 512 MiB memory, four concurrent requests
- Access: Cloud Run Invoker is granted only to `oanagoge@gmail.com`; the application also requires secret version 2 from `foodlog-preview-shared-secret`
- Budget: DKK 400 monthly gross-spend alert, excluding credits, with current-spend notifications at 25%, 50%, and 75% (DKK 100, 200, and 300)

The authenticated smoke test created test account `60ae7914-b2af-4d73-9e6e-d54fee09f080` and browser camera `8b1175ad-5b50-42ed-b61a-2bef9aa8c87c`. It uploaded the synthetic steak and chicken fixtures, received `202 Accepted` for both, and read back two confident journal entries: `Air-fried steak` and `Air-fried chicken breast`. The private steak-image response had SHA-256 `8e51f9691aebf6335d6d1cf1c7863b73654918bb97db3bd99bd5235e290da208`, exactly matching the source fixture.

A later adversarial smoke test uploaded all three degraded distant-camera fixtures through browser camera `90b28f68-54c9-461c-92bf-e791736f1984`. All three were accepted and stored as provisional `Unrecognized kitchen activity` entries with `uncertain` confidence, and each produced one open clarification question. This is the required no-model behavior: the fixture hashes are intentionally absent from the deterministic map, so the preview does not pretend to evaluate visual recognition while Gemini remains disabled.

Cloud Logging showed eight successful smoke/verification requests, one expected diagnostic `401` while correcting the initial secret encoding, and no `5xx` responses. The Cloud Run dashboard populated application request latency around 7-28 ms and end-to-end latency around 25-372 ms for this tiny sample. Its Cost view was left unavailable because it requests the additional App Optimize API, which was not needed for verification and was not enabled.

This test state is ephemeral. The account, camera, images, and journal entries disappear whenever the single in-memory instance is replaced or scales to zero. The identifiers above are deployment evidence, not durable fixtures or credentials. Secrets and bearer tokens are never recorded.

## Redeployment invariants

- Generate the Secret Manager value without a trailing newline. For example, pipe `openssl rand -hex 32` through `tr -d '\n'` before adding the secret version.
- Set both service-level scaling (`--min=0 --max=1`) and revision-level scaling (`--min-instances=0 --max-instances=1`). Cloud Run exposes these as distinct controls; setting only the revision flags leaves the service-level maximum at its default.
- Pin the deployed revision to an explicit secret version after validating it. Do not depend on `latest` for a verified release.
- Run the authenticated account, capture, journal, and private-image checks after every deployment. A ready revision alone is not sufficient evidence.
