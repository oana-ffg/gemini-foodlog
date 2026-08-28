# Production infrastructure

This root owns the durable Google Cloud resources for Gemini FoodLog in project
`gemini-foodlog-2026`. It uses a private GCS backend so plans and applies share a
versioned, lockable source of truth.

## State bootstrap

The backend bucket must exist before Terraform can initialize. It was bootstrapped
once with the Google Cloud CLI using these invariants:

- bucket: `gemini-foodlog-2026-tfstate-163029863855`;
- location: `EU`;
- uniform bucket-level access;
- public-access prevention enforced;
- object versioning enabled;
- seven-day soft-delete recovery.

The bucket is represented by `google_storage_bucket.terraform_state` and imported
into its own backend after initialization. `prevent_destroy` protects it from an
ordinary Terraform destroy or replacement.

## Local verification

Use Google application-default credentials for the project owner, then run:

```bash
terraform init
terraform fmt -check
terraform validate
terraform plan -detailed-exitcode
```

Exit code `0` from the plan means the checked-in configuration matches live state.
Exit code `2` means Terraform found changes and the plan must be reviewed before an
apply. Never use `-auto-approve` for this production root.

## Production boundaries

This root is intentionally fixed to the `production` environment:

- regional services: `europe-west1`;
- Firestore: `eur3`;
- durable private object storage: `EU`.

`services.tf` owns the minimum API set required by the durable capture spine.
Service resources use `disable_on_destroy = false` so removing Terraform state or
retiring this root cannot unexpectedly disable shared Google Cloud APIs.

The default Firestore database is provisioned in Native mode in `eur3`. Terraform
uses deletion policy `PREVENT`, and the live database also has delete protection
enabled. Point-in-time recovery remains disabled during the prototype to avoid
unnecessary retained-version storage; the standard one-hour version window remains.

### Imported historical preview resources

`historical_preview.tf` owns the private preview resources that existed before this
production root: the gross-spend billing budget, source-deploy Artifact Registry
repository, preview runner identity, preview secret metadata and accessor IAM, and
the isolated preview Cloud Run service and invoker IAM. They were imported into the
shared remote state without replacement. The service preserves its original pinned
image and source-build provenance and remains scale-to-zero at revision
`foodlog-preview-api-00002-clf`.

Terraform deliberately owns only the preview secret metadata and access policy; its
payload versions remain in Secret Manager and never enter configuration or state.
Deletion prevention protects the historical resources.

### Artifact retention and rollback protection

The source-deploy repository has an active Terraform-owned cleanup policy. Versions
must be at least 14 days old before deletion, the newest 10 versions of every package
are always retained, and any tag beginning with `protected-` wins over deletion.
Before activation, the exact policy was applied and read back in dry-run mode; the
full live inventory had zero deletion candidates.

The current production digest, the last known-good OPS-005 rollback digest, and the
historical preview digest have immutable-digest protection tags. Every deployment
must promote its predecessor to a `protected-rollback-*` tag and tag the new verified
digest `protected-active-*` before it can become older than the retention window.
CI owns automating that rotation; until CI exists, this is a required manual release
step. Never infer protected digests from mutable tags: compare the digest against all
active Cloud Run services and jobs first.

### Unlimited internal or judge accounts

Public accounts receive the configured 200-image lifetime trial. To make a known
Firebase account unlimited, add its exact UID to an ignored
`infra/terraform/production/terraform.tfvars` file as an item in the
`unlimited_owner_user_ids` set. Create that input only after the real UID is known;
do not commit the file or the UID. Apply the reviewed Terraform plan before the
account is provisioned. Entitlement mode is assigned when the account record is
created and is not silently changed by later configuration edits.

## App Engine inbound-mail bootstrap

The App Engine application is a one-time project property rather than a resource in
the pinned Google Terraform provider. It was initialized on 25 August 2026 in
`europe-west`, the App Engine location that maps to the existing `eur3` Firestore
multi-region. The location cannot be changed. The app-level default identity is the
dedicated keyless `foodlog-mail@gemini-foodlog-2026.iam.gserviceaccount.com` service
account and the app-level SSL policy requires TLS 1.2.

The exact completed bootstrap command was:

```sh
gcloud app create \
  --project=gemini-foodlog-2026 \
  --region=europe-west \
  --service-account=foodlog-mail@gemini-foodlog-2026.iam.gserviceaccount.com \
  --ssl-policy=TLS_VERSION_1_2 \
  --quiet
```

Terraform owns the required App Engine Admin API but deliberately does not attempt to
recreate or relocate the application. The bounded default-service MIME gateway is
deployed separately from `services/mail-gateway`.

Initialization also created the platform-managed EU buckets
`gemini-foodlog-2026.appspot.com` and `staging.gemini-foodlog-2026.appspot.com`.
Neither policy contains a public principal. They retain the App Engine defaults for
code and deployment staging only; raw inbound messages must use the separate private
raw-mail bucket above. The staging bucket deletes live objects after fifteen days.
App Engine runs its managed source build as the selected version service account, so
Terraform grants `foodlog-mail` Object Admin plus legacy bucket-reader access on this
staging bucket, Artifact Registry writer on only the platform-created `gae-standard`
repository, and project log-writer. These are build-time grants: the identity cannot
change the staging bucket or its IAM, write the Cloud Run repository, or administer
builds.

Google also created the unused App Engine default service account with project Editor.
That automatic grant was removed immediately after verifying that the app-level
default is `foodlog-mail` and the default account has no user-managed keys. The
pre-existing Compute default account remains the identity used
by manual Cloud Build; replacing it is part of the repository-scoped Workload Identity
Federation and CI deployment work, not this bootstrap.

## Private object storage

`storage.tf` owns four application buckets. All are `EU` multi-region buckets with
uniform bucket-level access and public-access prevention enforced:

- `gemini-foodlog-2026-media-163029863855` stores uploaded images;
- `gemini-foodlog-2026-raw-mail-163029863855` stores original inbound messages;
- `gemini-foodlog-2026-traces-163029863855` stores full agent traces;
- `gemini-foodlog-2026-exports-163029863855` stores temporary user exports.

Images, raw mail, and traces intentionally have no deletion lifecycle during the
prototype and retain deleted objects for seven days as accidental-deletion recovery.
The export bucket deletes live objects after one day and disables soft delete so a
temporary export does not remain recoverable after its lifecycle expires. Bucket IAM
is authoritative. The project owner administers the buckets; API, worker, and mail
identities receive only the object create/read operations required by their data
flow. None can change bucket policy or delete retained objects.

## Keyless runtime identities

`identities.tf` creates separate API, worker, mail, notification, CI infrastructure,
and CI deployment service accounts. Runtime data-plane accounts receive Firestore
user access, while bucket roles are assigned per data flow in `storage.tf`. Terraform
does not create service-account keys. The CI identities remain unusable from GitHub
for deployment until INF-016 grants the exact resource operations required by its
workflows.

### Keyless GitHub Actions trust

`workload_identity.tf` owns an API-deletion-protected Workload Identity pool and OIDC
provider for GitHub Actions. The provider requires all of these claims together:

- immutable repository ID `1343967496` and owner ID `212630009`;
- immutable subject prefix for `oana-ffg/gemini-foodlog`;
- exact `refs/heads/main` ref;
- exact `production` job environment.

Only that repository-ID principal set may impersonate the dedicated CI infrastructure
and deployment accounts, and neither binding creates a service-account key. GitHub's
repository-level immutable subject option is enabled. Its `production` environment
allows only `main`, requires Oana's approval, permits Oana to approve her own release,
and forbids administrator bypass. Pull-request checks must not reference this
environment or request an OIDC token. The first real accepted/rejected token exchange
is exercised by INF-016's workflow smoke; static readback alone does not claim it.

## Firebase foundation

`firebase.tf` adds Firebase to the existing `gemini-foodlog-2026` Google Cloud
project; it never creates a second project. Firebase Management, Hosting, App Check,
and Identity Toolkit APIs are explicit Terraform-owned services. Identity Platform
enables email/password sign-in, forbids duplicate emails, disables anonymous and
phone sign-in, and authorizes only localhost plus the project's Firebase domains.
The web app, restricted browser API key, and Hosting release are created by their
dedicated authentication and UI tasks. The App Check API remains available, but
provider configuration and enforcement are deliberately deferred until a hard
assessment-spend boundary exists.

## Production API

`cloud_run.tf` owns the browser-reachable API transport. The application remains
authenticated: every `/v1` route derives its account from a verified Firebase ID
token or a revocable camera credential, while `/health` is intentionally public for
platform probes. The service runs with the dedicated API identity, request-only CPU,
zero minimum instances, one maximum instance, eight concurrent requests per instance,
a 60-second timeout, and no model configuration.

The deploy input in `production.auto.tfvars` must be an Artifact Registry image pinned
by sha256 digest. Release work updates that one reviewed value after tests and the
remote image build succeed; mutable tags are never deployed.

## Operational telemetry

`monitoring.tf` owns eight bounded log-based application metrics. Native Cloud Run,
Pub/Sub, and Cloud Storage metrics cover service health, queue/DLQ backlog, and
private object storage without duplicating those points. The complete signal map,
cardinality boundary, cost boundary, and live verification contract are documented
in [`docs/observability.md`](../../../docs/observability.md).

## Runtime secrets

`secrets.tf` owns protected, regional Secret Manager metadata and narrow accessor IAM;
it never owns secret payloads. Pushover values are streamed from gopass directly to
`gcloud secrets versions add --data-file=-`, so they never appear in source, process
arguments, environment files, Terraform plans, or Terraform state. Only the dedicated
notification service account can read these two secrets. A deployed worker must pin
an explicit enabled version rather than resolving `latest` at runtime.
