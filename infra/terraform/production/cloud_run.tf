variable "api_container_image" {
  description = "Immutable Artifact Registry digest deployed by the production API service."
  type        = string

  validation {
    condition = can(regex(
      "^europe-west1-docker\\.pkg\\.dev/gemini-foodlog-2026/cloud-run-source-deploy/foodlog-api@sha256:[0-9a-f]{64}$",
      var.api_container_image,
    ))
    error_message = "The production API image must be the expected Artifact Registry repository pinned by sha256 digest."
  }
}

locals {
  api_runtime_environment = {
    FOODLOG_ALLOWED_ORIGINS = jsonencode([
      "https://gemini-foodlog-2026.firebaseapp.com",
      "https://gemini-foodlog-2026.web.app",
    ])
    FOODLOG_AUTH_BACKEND                  = "firebase"
    FOODLOG_ENVIRONMENT                   = var.environment
    FOODLOG_FIREBASE_PROJECT_ID           = var.project_id
    FOODLOG_GCP_PROJECT_ID                = var.project_id
    FOODLOG_LAUNCH_CONSENT_POLICY_VERSION = "launch-interest-v1"
    FOODLOG_MEDIA_BUCKET                  = google_storage_bucket.retained["media"].name
    FOODLOG_PUBLIC_ACCOUNT_LIMIT          = "25"
    FOODLOG_STORAGE_BACKEND               = "gcp"
    FOODLOG_TRIAL_IMAGE_LIMIT             = "200"
    FOODLOG_WAITLIST_POLICY_VERSION       = "capacity-waitlist-v1"
  }
}

resource "google_cloud_run_v2_service" "api" {
  project  = var.project_id
  name     = "foodlog-api"
  location = var.region

  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"

  labels = merge(local.common_labels, {
    component = "api"
  })

  template {
    service_account                  = google_service_account.runtime["api"].email
    timeout                          = "60s"
    max_instance_request_concurrency = 8

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      name  = "api"
      image = var.api_container_image

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
        startup_cpu_boost = false
      }

      dynamic "env" {
        for_each = local.api_runtime_environment

        content {
          name  = env.key
          value = env.value
        }
      }

      startup_probe {
        initial_delay_seconds = 0
        timeout_seconds       = 5
        period_seconds        = 5
        failure_threshold     = 12

        http_get {
          path = "/health"
          port = 8080
        }
      }

      liveness_probe {
        initial_delay_seconds = 10
        timeout_seconds       = 5
        period_seconds        = 30
        failure_threshold     = 3

        http_get {
          path = "/health"
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
    google_project_service.required["run.googleapis.com"],
  ]
}

# Cloud Run must accept browser transport without Google IAM credentials. Every
# application endpoint derives its tenant from Firebase or a revocable camera
# credential; /health is the only intentionally unauthenticated application route.
resource "google_cloud_run_v2_service_iam_member" "public_api_transport" {
  project  = var.project_id
  location = google_cloud_run_v2_service.api.location
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "production_api_url" {
  description = "Public transport URL for the Firebase-authenticated production API."
  value       = google_cloud_run_v2_service.api.uri
}
