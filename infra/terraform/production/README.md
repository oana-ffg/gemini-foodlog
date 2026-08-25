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
until repository-scoped Workload Identity Federation and deployment resources are
defined by their dependent backlog tasks.
