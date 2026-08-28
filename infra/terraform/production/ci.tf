locals {
  ci_deploy_services = {
    api          = google_cloud_run_v2_service.api.name
    export       = google_cloud_run_v2_service.export_worker.name
    image        = google_cloud_run_v2_service.image.name
    inference    = google_cloud_run_v2_service.inference.name
    mail         = google_cloud_run_v2_service.mail_worker.name
    notification = google_cloud_run_v2_service.notification.name
  }

  ci_deploy_jobs = {
    adk_agent            = google_cloud_run_v2_job.adk_agent_smoke.name
    context_tools        = google_cloud_run_v2_job.context_tools_smoke.name
    event_evidence       = google_cloud_run_v2_job.event_evidence_smoke.name
    firestore_repository = google_cloud_run_v2_job.firestore_repository_smoke.name
    model_spend          = google_cloud_run_v2_job.model_spend_smoke.name
    vertex_access        = google_cloud_run_v2_job.vertex_access_smoke.name
  }
}

# CI planning uses -refresh=false and reads only the remote state. The deployment
# identity additionally needs the lock and state-object write operations below.
resource "google_project_iam_custom_role" "terraform_state_reader" {
  project     = var.project_id
  role_id     = "foodlogTerraformStateReader"
  title       = "FoodLog Terraform state reader"
  description = "Read the exact FoodLog Terraform backend without project resource access."
  permissions = [
    "storage.buckets.get",
    "storage.objects.get",
    "storage.objects.list",
  ]
  stage = "GA"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_custom_role" "terraform_state_writer" {
  project     = var.project_id
  role_id     = "foodlogTerraformStateWriter"
  title       = "FoodLog Terraform state writer"
  description = "Read, lock, and update the exact FoodLog Terraform backend."
  permissions = [
    "storage.buckets.get",
    "storage.objects.create",
    "storage.objects.delete",
    "storage.objects.get",
    "storage.objects.list",
    "storage.objects.update",
  ]
  stage = "GA"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_custom_role" "ci_service_user" {
  project     = var.project_id
  role_id     = "foodlogCiServiceUser"
  title       = "FoodLog CI service user"
  description = "Charge approved CI API calls to only the FoodLog project."
  permissions = ["serviceusage.services.use"]
  stage       = "GA"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_member" "ci_deploy_service_user" {
  project = var.project_id
  role    = google_project_iam_custom_role.ci_service_user.name
  member  = "serviceAccount:${google_service_account.runtime["ci_deploy"].email}"
}

resource "google_cloud_run_v2_service_iam_member" "ci_deploy" {
  for_each = local.ci_deploy_services

  project  = var.project_id
  location = var.region
  name     = each.value
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.runtime["ci_deploy"].email}"
}

resource "google_cloud_run_v2_job_iam_member" "ci_deploy" {
  for_each = local.ci_deploy_jobs

  project  = var.project_id
  location = var.region
  name     = each.value
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.runtime["ci_deploy"].email}"
}

resource "google_artifact_registry_repository_iam_member" "ci_deploy_backend_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.backend.location
  repository = google_artifact_registry_repository.backend.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.runtime["ci_deploy"].email}"
}
