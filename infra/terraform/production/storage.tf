locals {
  retained_buckets = {
    media = {
      name    = "gemini-foodlog-2026-media-163029863855"
      purpose = "private-media"
    }
    raw_mail = {
      name    = "gemini-foodlog-2026-raw-mail-163029863855"
      purpose = "private-raw-mail"
    }
    traces = {
      name    = "gemini-foodlog-2026-traces-163029863855"
      purpose = "private-ai-traces"
    }
  }
}

resource "google_storage_bucket" "retained" {
  for_each = local.retained_buckets

  name                        = each.value.name
  project                     = var.project_id
  location                    = var.storage_location
  storage_class               = "STANDARD"
  public_access_prevention    = "enforced"
  uniform_bucket_level_access = true

  labels = merge(local.common_labels, {
    purpose = each.value.purpose
  })

  soft_delete_policy {
    retention_duration_seconds = 604800
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["storage.googleapis.com"],
  ]
}

resource "google_storage_bucket_iam_policy" "retained" {
  for_each = google_storage_bucket.retained

  bucket      = each.value.name
  policy_data = data.google_iam_policy.retained_bucket[each.key].policy_data
}

# App Engine's secure-by-default managed build uses the configured version identity
# to read source and finish its GCS log object. Scope Google's required Object Admin
# role to this platform staging bucket; it cannot modify bucket settings or IAM.
resource "google_storage_bucket_iam_member" "app_engine_staging_objects" {
  bucket = "staging.${var.project_id}.appspot.com"
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime["mail"].email}"
}

resource "google_storage_bucket_iam_member" "app_engine_staging_bucket_reader" {
  bucket = "staging.${var.project_id}.appspot.com"
  role   = "roles/storage.legacyBucketReader"
  member = "serviceAccount:${google_service_account.runtime["mail"].email}"
}

data "google_iam_policy" "retained_bucket" {
  for_each = local.retained_buckets

  binding {
    role = "roles/storage.admin"

    members = [
      "user:oanagoge@gmail.com",
    ]
  }

  dynamic "binding" {
    for_each = each.key == "media" ? [1] : []

    content {
      role = "roles/storage.objectCreator"

      members = [
        "serviceAccount:${google_service_account.runtime["api"].email}",
      ]
    }
  }

  dynamic "binding" {
    for_each = each.key == "media" ? [1] : []

    content {
      role = "roles/storage.objectViewer"

      members = [
        "serviceAccount:${google_service_account.runtime["api"].email}",
        "serviceAccount:${google_service_account.runtime["worker"].email}",
      ]
    }
  }

  dynamic "binding" {
    for_each = each.key == "raw_mail" ? [1] : []

    content {
      role = "roles/storage.objectCreator"

      members = [
        "serviceAccount:${google_service_account.runtime["mail"].email}",
      ]
    }
  }

  dynamic "binding" {
    for_each = each.key == "raw_mail" ? [1] : []

    content {
      role = "roles/storage.objectViewer"

      members = [
        "serviceAccount:${google_service_account.runtime["worker"].email}",
      ]
    }
  }

  dynamic "binding" {
    for_each = each.key == "traces" ? [1] : []

    content {
      role = "roles/storage.objectCreator"

      members = [
        "serviceAccount:${google_service_account.runtime["worker"].email}",
      ]
    }
  }

  dynamic "binding" {
    for_each = each.key == "traces" ? [1] : []

    content {
      role = "roles/storage.objectViewer"

      members = [
        "serviceAccount:${google_service_account.runtime["api"].email}",
      ]
    }
  }
}

resource "google_storage_bucket" "exports" {
  name                        = "gemini-foodlog-2026-exports-163029863855"
  project                     = var.project_id
  location                    = var.storage_location
  storage_class               = "STANDARD"
  public_access_prevention    = "enforced"
  uniform_bucket_level_access = true

  labels = merge(local.common_labels, {
    purpose = "private-temporary-exports"
  })

  lifecycle_rule {
    action {
      type = "Delete"
    }

    condition {
      age        = 1
      with_state = "LIVE"
    }
  }

  soft_delete_policy {
    retention_duration_seconds = 0
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["storage.googleapis.com"],
  ]
}

resource "google_storage_bucket_iam_policy" "exports" {
  bucket      = google_storage_bucket.exports.name
  policy_data = data.google_iam_policy.exports.policy_data
}

data "google_iam_policy" "exports" {
  binding {
    role = "roles/storage.admin"

    members = [
      "user:oanagoge@gmail.com",
    ]
  }

  binding {
    role = "roles/storage.objectCreator"

    members = [
      "serviceAccount:${google_service_account.runtime["api"].email}",
    ]
  }

  binding {
    role = "roles/storage.objectViewer"

    members = [
      "serviceAccount:${google_service_account.runtime["api"].email}",
    ]
  }
}
