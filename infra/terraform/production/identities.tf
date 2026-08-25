locals {
  service_accounts = {
    api = {
      account_id   = "foodlog-api"
      display_name = "FoodLog production API"
    }
    worker = {
      account_id   = "foodlog-worker"
      display_name = "FoodLog production worker"
    }
    mail = {
      account_id   = "foodlog-mail"
      display_name = "FoodLog inbound mail gateway"
    }
    notification = {
      account_id   = "foodlog-notify"
      display_name = "FoodLog notification worker"
    }
    ci_infra = {
      account_id   = "foodlog-ci-infra"
      display_name = "FoodLog CI infrastructure deployment"
    }
    ci_deploy = {
      account_id   = "foodlog-ci-deploy"
      display_name = "FoodLog CI application deployment"
    }
  }

  datastore_runtime_accounts = toset([
    "api",
    "mail",
    "notification",
    "worker",
  ])

  deployable_runtime_accounts = toset([
    "api",
    "mail",
    "notification",
    "worker",
  ])
}

resource "google_service_account" "runtime" {
  for_each = local.service_accounts

  project      = var.project_id
  account_id   = each.value.account_id
  display_name = each.value.display_name

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["iam.googleapis.com"],
  ]
}

# Firestore does not support collection-scoped IAM. Account isolation therefore
# remains a mandatory repository invariant, while each data-plane process gets the
# narrow predefined role needed to use the database rather than project Editor.
resource "google_project_iam_member" "datastore_runtime" {
  for_each = local.datastore_runtime_accounts

  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.runtime[each.value].email}"
}

# The deploy identity may attach only the explicit runtime identities to Cloud Run.
# Workload Identity Federation and the remaining deployment grants are intentionally
# added with INF-015/INF-016, when their repository and resource scopes exist.
resource "google_service_account_iam_member" "ci_deploy_can_attach_runtime" {
  for_each = local.deployable_runtime_accounts

  service_account_id = google_service_account.runtime[each.value].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.runtime["ci_deploy"].email}"
}

output "runtime_service_account_emails" {
  description = "Keyless identities used by production workloads and deployment automation."
  value = {
    for key, account in google_service_account.runtime : key => account.email
  }
}
