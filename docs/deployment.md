# Reproducible setup and deployment

- **Last rehearsed:** 2026-08-28
- **Production project:** `gemini-foodlog-2026` (`163029863855`)
- **Primary region:** `europe-west1`
- **Firestore location:** `eur3`
- **Storage location:** `EU`

This repository describes one real hackathon deployment. The production Terraform root intentionally validates the exact project, repository, and immutable image path; it is not a generic click-to-create template for an arbitrary Google Cloud project. That constraint prevents an accidental second production environment or silent use of a different billing account.

## Toolchain

The locked CI baseline is:

- Node.js 24 and `npm ci` from `package-lock.json`;
- Python 3.13 in CI, with packages supporting Python 3.12 or newer;
- uv 0.5.13 and each package's committed `uv.lock`;
- Terraform 1.15.8 and the committed cross-platform provider lock;
- Google Cloud CLI for operator inspection;
- Firebase CLI 15.28.1, invoked through the locked root scripts;
- Java for the Firestore Rules emulator test.

Docker is required only for the protected backend release workflow or an equivalent reviewed container build. No service-account key file is used; GitHub production access uses Workload Identity Federation.

## Clean checkout verification

From the repository root:

```bash
npm ci
npm run typecheck
npm test --workspace @foodlog/web
npm run build
```

Verify the backend:

```bash
cd services/backend
uv sync --frozen
uv run ruff check .
uv run pytest
uv build
```

Verify the inbound-mail gateway and camera client independently:

```bash
cd services/mail-gateway
uv sync --frozen
uv run ruff check .
uv run pytest
uv build

cd ../../clients/python
uv sync --frozen
uv run ruff check .
uv run pytest
uv build
```

Verify infrastructure syntax without reading or changing production state:

```bash
cd ../../infra/terraform/production
terraform fmt -check
terraform init -backend=false -input=false
terraform validate
```

The complete direct-client Firestore Rules denial suite requires Java and the Firebase emulator:

```bash
cd ../../..
npm run test:firestore-rules
```

The ordinary GitHub CI workflow performs the web, backend, and backend-disabled Terraform checks from a clean Ubuntu checkout on every pull request and push to `main`.

## Local development modes

### Deterministic backend-only mode

`services/backend/.env.example` is safe to copy to `services/backend/.env`. Its default local-authentication and in-memory-storage settings do not contact Gemini, Pub/Sub, Firestore, or Cloud Storage.

```bash
cd services/backend
cp .env.example .env
uv sync --frozen
uv run uvicorn foodlog_backend.main:app --port 8080
```

Local-authenticated API requests must send `X-FoodLog-Local-User`; this mode is intended for API development and deterministic tests, not the React sign-in flow.
Interactive API documentation is available only in this local mode at
`http://127.0.0.1:8080/docs`. Preview and production disable `/docs`, `/redoc`,
`/docs/oauth2-redirect`, and `/openapi.json`; the checked-in generated contract remains
[`contracts/openapi.json`](../contracts/openapi.json).

The IAM-private historical preview remains pinned to its immutable 25 Aug image
for provenance and is not rebuilt by current releases. Its legacy FastAPI
framework routes are therefore part of that isolated historical artifact, not
the current preview-mode or production route contract.

### React plus local in-memory API

The React application always uses real Firebase ID tokens. To exercise it against an ephemeral in-memory local API, copy `.env.example` as above and change `FOODLOG_AUTH_BACKEND=local` to `FOODLOG_AUTH_BACKEND=firebase`. Keep `FOODLOG_ENVIRONMENT=local` and `FOODLOG_STORAGE_BACKEND=memory`; that prevents Cloud storage, Pub/Sub publication, and Gemini processing. The checked-in public Firebase web configuration is restricted to the FoodLog domains and localhost.

Run the backend from `services/backend`, then run the web application from the repository root:

```bash
npm ci
npm run dev:web
```

Open `http://127.0.0.1:5173` and sign in with a verified FoodLog Firebase identity. Local in-memory data disappears when the backend process stops. A browser capture can be accepted locally, but no background grouping or model worker runs in this mode.

## Production configuration boundary

The canonical production inputs are source-controlled in `infra/terraform/production`:

- `production.auto.tfvars` pins the backend image by immutable SHA-256 digest;
- `cloud_run.tf` fixes the 25-account gate, 200-image trial, DKK 400 model ceiling, scale-to-zero bounds, Gemini model, and allowed hosted origins;
- `backend.tf` pins the private GCS Terraform state bucket;
- `ci.tf` defines the keyless plan/deploy identities and their narrow permissions;
- `firebase.json`, Firestore indexes, and Firestore Rules define the browser-facing Firebase surface.

The only sensitive Terraform input is `unlimited_owner_user_ids`. Supply real Firebase UIDs only through ignored local `terraform.tfvars`; never commit identities. Pushover payloads live only in enabled Secret Manager versions for `foodlog-pushover-app-token` and `foodlog-pushover-user-key`. The Terraform configuration creates and authorizes the secret resources but does not contain their values. Camera credentials, user passwords, inbound-mail addresses, test-account identities, and promotion details also stay out of source and workflow inputs.

## Read-only production plan

Local operator planning uses Application Default Credentials for Oana's authorized Google identity:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project gemini-foodlog-2026
cd infra/terraform/production
terraform init -input=false
terraform plan
```

Do not apply an unreviewed plan. Normal plan verification uses the protected GitHub workflow, whose plan identity can read only the exact state backend and cannot mint credentials outside the protected production environment:

```bash
gh workflow run production.yml --ref main -f operation=plan
```

The production environment requires Oana's GitHub approval before the protected job can authenticate.

## Backend release

The protected workflow is the canonical backend deployment path:

```bash
gh workflow run production.yml --ref main -f operation=deploy
```

For the exact approved `main` commit, it:

1. proves an unprotected job cannot exchange a Google token;
2. reruns locked web/backend/Terraform quality gates;
3. builds and pushes the backend container;
4. resolves and validates its immutable Artifact Registry digest;
5. accepts only an in-place plan limited to the six Cloud Run services and six smoke jobs;
6. commits the immutable release input locally;
7. applies the saved plan and waits for every Cloud Run operation;
8. pushes the release-input commit only after apply succeeds;
9. moves the protected rollback and active tags;
10. verifies API health, exact service/job digests, the active tag, and final zero Terraform drift.

The workflow never deploys from a floating tag. Do not manually move traffic or protected tags around this process.

## Firebase release

Web hosting, Firestore indexes, and direct-client denial rules have separate explicit commands:

```bash
npm run deploy:web
npm run deploy:indexes
npm run deploy:rules
```

Each command targets `gemini-foodlog-2026`. Review the built web bundle and relevant Firebase diff before release. After hosting, verify the root and direct `/camera`, `/context`, `/knowledge`, `/purchases`, and `/data` routes over HTTPS in a fresh signed-out session, then perform authenticated checks only with the dedicated test or judge account.

## Inbound-mail release

The App Engine Standard default service is the only inbound-mail transport. Follow `services/mail-gateway/README.md`: build and test its locked package, review `app.yaml`, deploy a unique non-promoted version under the dedicated `foodlog-mail` identity, inspect configuration and logs, and move traffic only after an actual App Engine inbound-mail smoke succeeds. An ordinary external HTTP POST is not a valid mail-transport test. Never send private mailbox contents as a fixture.

## Post-release evidence

A backend release is complete only after all of the following are true:

- `/health` returns `{"status":"ok","mode":"production"}` from `https://foodlog-api-sptvo5nsga-ew.a.run.app/health`;
- all six production services and all six retained smoke jobs are Ready on the committed immutable digest;
- `protected-active` resolves to that digest and `protected-rollback` resolves to the previous verified release;
- Terraform reports no drift;
- release-window error logs contain no unexplained errors;
- a bounded authenticated product smoke proves the changed behavior with persisted evidence;
- `docs/mvp-backlog.html` records exact run IDs, resource IDs, trace IDs, and remaining human/long tests without upgrading incomplete work to Done.

Firebase-only releases additionally require direct-route, signed-out privacy-boundary, bundle, and console-log checks. Inbound-mail releases require real transport evidence plus raw-object, Firestore, Pub/Sub, and worker correlation.

## Recovery and cost boundary

If apply fails, inspect live service/job digests before retrying because Cloud Run operations may have been accepted even when the caller could not observe completion. Never reset Terraform state or move protected tags speculatively. The reviewed recovery digest is `protected-rollback`; use it only through an exact reviewed plan.

Cloud Billing alerts are not a hard cap. The DKK 400 Gemini ceiling is separately enforced in Firestore before calls, and the promotion expires on 2026-09-24. Follow [credit-expiry-runbook.md](credit-expiry-runbook.md) and [judge-availability-runbook.md](judge-availability-runbook.md); never raise limits, delete retained user data, disable billing, or accept out-of-pocket spend without Oana's explicit decision.
