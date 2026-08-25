resource "google_storage_bucket" "terraform_state" {
  name     = "gemini-foodlog-2026-tfstate-163029863855"
  project  = var.project_id
  location = "EU"

  public_access_prevention    = "enforced"
  uniform_bucket_level_access = true

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

data "google_iam_policy" "terraform_state" {
  binding {
    role = "roles/storage.admin"

    members = [
      "user:oanagoge@gmail.com",
    ]
  }
}

resource "google_storage_bucket_iam_policy" "terraform_state" {
  bucket      = google_storage_bucket.terraform_state.name
  policy_data = data.google_iam_policy.terraform_state.policy_data
}
