resource "google_storage_bucket" "terraform_state" {
  name     = "gemini-foodlog-2026-tfstate-163029863855"
  project  = var.project_id
  location = "EU"

  public_access_prevention    = "enforced"
  uniform_bucket_level_access = true

  labels = merge(local.common_labels, {
    purpose = "terraform-state"
  })

  versioning {
    enabled = true
  }

  soft_delete_policy {
    retention_duration_seconds = 604800
  }

  lifecycle {
    prevent_destroy = true
  }
}

data "google_iam_policy" "oana_storage_admin" {
  binding {
    role = "roles/storage.admin"

    members = [
      "user:oanagoge@gmail.com",
    ]
  }

  binding {
    role = google_project_iam_custom_role.terraform_state_reader.name

    members = [
      "serviceAccount:${google_service_account.runtime["ci_infra"].email}",
    ]
  }

  binding {
    role = google_project_iam_custom_role.terraform_state_writer.name

    members = [
      "serviceAccount:${google_service_account.runtime["ci_deploy"].email}",
    ]
  }
}

resource "google_storage_bucket_iam_policy" "terraform_state" {
  bucket      = google_storage_bucket.terraform_state.name
  policy_data = data.google_iam_policy.oana_storage_admin.policy_data
}

moved {
  from = data.google_iam_policy.terraform_state
  to   = data.google_iam_policy.oana_storage_admin
}
