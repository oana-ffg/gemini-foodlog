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
