variable "billing_account_id" {
  description = "Billing account that owns the existing FoodLog project budget."
  type        = string
  default     = "010F14-E316E1-5D2C4F"
}

locals {
  preview_image = "europe-west1-docker.pkg.dev/gemini-foodlog-2026/cloud-run-source-deploy/foodlog-preview-api@sha256:6be2dbd229d78e4407bf54c25a9ad38b1653bc9e6c39c3c9e2021bdd50efdae4"
}

# This budget predates the production Terraform root. It measures gross project
# spend so promotional credits cannot hide an approaching out-of-pocket charge.
resource "google_billing_budget" "foodlog" {
  billing_account = var.billing_account_id
  display_name    = "FoodLog private preview"
  deletion_policy = "PREVENT"

  amount {
    specified_amount {
      currency_code = "DKK"
      units         = "400"
    }
  }

  budget_filter {
    projects               = ["projects/${var.project_number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  threshold_rules {
    threshold_percent = 0.25
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.50
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.75
    spend_basis       = "CURRENT_SPEND"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["billingbudgets.googleapis.com"],
  ]
}

# Cloud Run source deploy created this repository before Terraform existed. It
# now stores both the isolated historical preview and current backend images.
resource "google_artifact_registry_repository" "backend" {
  project       = var.project_id
  location      = var.region
  repository_id = "cloud-run-source-deploy"
  description   = "Cloud Run Source Deployments"
  format        = "DOCKER"
  mode          = "STANDARD_REPOSITORY"

  deletion_policy = "PREVENT"

  # INF-011 first applied and verified this exact policy in dry-run mode. No
  # version becomes eligible until it is at least 14 days old.
  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "delete-old-versions"
    action = "DELETE"

    condition {
      tag_state  = "ANY"
      older_than = "1209600s" # 14 days
    }
  }

  cleanup_policies {
    id     = "keep-protected-releases"
    action = "KEEP"

    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["protected-"]
    }
  }

  cleanup_policies {
    id     = "keep-recent-rollback-window"
    action = "KEEP"

    most_recent_versions {
      keep_count = 10
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["artifactregistry.googleapis.com"],
  ]
}

resource "google_service_account" "preview" {
  project      = var.project_id
  account_id   = "foodlog-preview-runner"
  display_name = "FoodLog private preview runner"

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["iam.googleapis.com"],
  ]
}

# Terraform owns only this secret's metadata and access policy. Its historical
# payload versions remain exclusively in Secret Manager and never enter state.
resource "google_secret_manager_secret" "preview" {
  project             = var.project_id
  secret_id           = "foodlog-preview-shared-secret"
  deletion_protection = true

  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["secretmanager.googleapis.com"],
  ]
}

resource "google_secret_manager_secret_iam_member" "preview_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.preview.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.preview.email}"
}

# This is retained only as immutable historical evidence for the early no-model
# preview. It is isolated from the production data path and scales to zero.
resource "google_cloud_run_v2_service" "preview" {
  project        = var.project_id
  name           = "foodlog-preview-api"
  location       = var.region
  client         = "gcloud"
  client_version = "545.0.0"

  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"

  # Preserve the exact one-time source deployment provenance imported from
  # Cloud Run. Future production builds use the separately managed image path.
  build_config {
    enable_automatic_updates = false
    image_uri                = "europe-west1-docker.pkg.dev/gemini-foodlog-2026/cloud-run-source-deploy/foodlog-preview-api"
    source_location          = "gs://run-sources-gemini-foodlog-2026-europe-west1/services/foodlog-preview-api/1787650583.477445-102049413aca4112bd4ae27ff9b009d2.zip#1787650601116039"
  }

  template {
    service_account                  = google_service_account.preview.email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = "60s"
    max_instance_request_concurrency = 4

    labels = {
      app         = "gemini-foodlog"
      environment = "preview"
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = local.preview_image

      ports {
        name           = "http1"
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }

        cpu_idle          = true
        startup_cpu_boost = true
      }

      env {
        name  = "FOODLOG_ENVIRONMENT"
        value = "preview"
      }

      env {
        name  = "FOODLOG_STORAGE_BACKEND"
        value = "memory"
      }

      env {
        name  = "FOODLOG_TRIAL_IMAGE_LIMIT"
        value = "200"
      }

      env {
        name  = "FOODLOG_PUBLIC_ACCOUNT_LIMIT"
        value = "25"
      }

      env {
        name = "FOODLOG_PREVIEW_SHARED_SECRET"

        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.preview.secret_id
            version = "2"
          }
        }
      }

      startup_probe {
        timeout_seconds   = 240
        period_seconds    = 240
        failure_threshold = 1

        tcp_socket {
          port = 8080
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_artifact_registry_repository.backend,
    google_secret_manager_secret_iam_member.preview_accessor,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "preview_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.preview.location
  name     = google_cloud_run_v2_service.preview.name
  role     = "roles/run.invoker"
  member   = "user:oanagoge@gmail.com"
}
