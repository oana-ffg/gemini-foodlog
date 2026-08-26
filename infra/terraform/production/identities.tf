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

# Foundation-model generation requires only prediction. A project custom role
# avoids the model/dataset administration permissions bundled into
# roles/aiplatform.user and is granted only to the image/agent worker.
resource "google_project_iam_custom_role" "vertex_model_invoker" {
  project     = var.project_id
  role_id     = "foodlogVertexModelInvoker"
  title       = "FoodLog Vertex model invoker"
  description = "Invoke a configured Vertex AI model without administering Vertex resources."
  permissions = ["aiplatform.endpoints.predict"]
  stage       = "GA"

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["aiplatform.googleapis.com"],
  ]
}

resource "google_project_iam_member" "worker_vertex_model_invoker" {
  project = var.project_id
  role    = google_project_iam_custom_role.vertex_model_invoker.name
  member  = "serviceAccount:${google_service_account.runtime["worker"].email}"
}

# App Engine uses the selected version identity for its managed Cloud Build. The
# grant is limited to writing build logs and the platform-created App Engine image
# repository; it cannot write the API's Cloud Run repository or administer builds.
resource "google_project_iam_member" "mail_build_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.runtime["mail"].email}"
}

resource "google_artifact_registry_repository_iam_member" "mail_app_engine_image_writer" {
  project    = var.project_id
  location   = var.region
  repository = "gae-standard"
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.runtime["mail"].email}"
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
