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
    FOODLOG_GROUPING_POLICY_VERSION       = "temporal-v1"
    FOODLOG_GROUPING_QUIET_SECONDS        = "30"
    FOODLOG_GROUPING_REOPEN_SECONDS       = "7200"
    FOODLOG_IMAGE_TOPIC                   = google_pubsub_topic.events["image"].id
    FOODLOG_LAUNCH_CONSENT_POLICY_VERSION = "launch-interest-v1"
    FOODLOG_MEDIA_BUCKET                  = google_storage_bucket.retained["media"].name
    FOODLOG_NOTIFICATION_TOPIC            = google_pubsub_topic.events["notification"].id
    FOODLOG_PUBLIC_ACCOUNT_LIMIT          = "25"
    FOODLOG_STORAGE_BACKEND               = "gcp"
    FOODLOG_TRIAL_IMAGE_LIMIT             = "200"
    FOODLOG_WAITLIST_POLICY_VERSION       = "capacity-waitlist-v1"
  }

  notification_runtime_environment = {
    FOODLOG_NOTIFICATION_ENVIRONMENT          = var.environment
    FOODLOG_NOTIFICATION_GCP_PROJECT_ID       = var.project_id
    FOODLOG_NOTIFICATION_PUBLIC_ACCOUNT_LIMIT = "25"
    FOODLOG_NOTIFICATION_TRIAL_IMAGE_LIMIT    = "200"
  }

  image_runtime_environment = {
    FOODLOG_IMAGE_ENVIRONMENT             = var.environment
    FOODLOG_IMAGE_GCP_PROJECT_ID          = var.project_id
    FOODLOG_IMAGE_GROUPING_POLICY_VERSION = "temporal-v1"
    FOODLOG_IMAGE_GROUPING_QUIET_SECONDS  = "30"
    FOODLOG_IMAGE_GROUPING_REOPEN_SECONDS = "7200"
    FOODLOG_IMAGE_PUBLIC_ACCOUNT_LIMIT    = "25"
    FOODLOG_IMAGE_TRIAL_IMAGE_LIMIT       = "200"
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

resource "google_cloud_run_v2_service" "image" {
  project  = var.project_id
  name     = "foodlog-image"
  location = var.region

  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  labels = merge(local.common_labels, {
    component = "image"
  })

  template {
    service_account                  = google_service_account.runtime["worker"].email
    timeout                          = "30s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      name    = "image"
      image   = var.api_container_image
      command = ["uvicorn"]
      args = [
        "foodlog_backend.image_worker_main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
      ]

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
        for_each = local.image_runtime_environment

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

# Pub/Sub identifies as the image worker identity and can invoke only the
# internal image service; no browser or public principal receives this role.
resource "google_service_account_iam_member" "pubsub_image_token_creator" {
  service_account_id = google_service_account.runtime["worker"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_project_service_identity.pubsub.member
}

resource "google_cloud_run_v2_service_iam_member" "image_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.image.location
  name     = google_cloud_run_v2_service.image.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runtime["worker"].email}"
}

resource "google_cloud_run_v2_service" "notification" {
  project  = var.project_id
  name     = "foodlog-notification"
  location = var.region

  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  labels = merge(local.common_labels, {
    component = "notification"
  })

  template {
    service_account                  = google_service_account.runtime["notification"].email
    timeout                          = "30s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      name    = "notification"
      image   = var.api_container_image
      command = ["uvicorn"]
      args = [
        "foodlog_backend.notification_main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
      ]

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
        for_each = local.notification_runtime_environment

        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "FOODLOG_NOTIFICATION_PUSHOVER_APP_TOKEN"

        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.notification["pushover_app_token"].secret_id
            version = "1"
          }
        }
      }

      env {
        name = "FOODLOG_NOTIFICATION_PUSHOVER_USER_KEY"

        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.notification["pushover_user_key"].secret_id
            version = "1"
          }
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
    google_secret_manager_secret_iam_member.notification_accessor,
  ]
}

# Pub/Sub mints an OIDC token identifying the notification runtime account. The
# service accepts only that authenticated principal; it has no allUsers binding.
resource "google_service_account_iam_member" "pubsub_notification_token_creator" {
  service_account_id = google_service_account.runtime["notification"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_project_service_identity.pubsub.member
}

resource "google_cloud_run_v2_service_iam_member" "notification_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.notification.location
  name     = google_cloud_run_v2_service.notification.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runtime["notification"].email}"
}

output "production_api_url" {
  description = "Public transport URL for the Firebase-authenticated production API."
  value       = google_cloud_run_v2_service.api.uri
}

output "notification_service_url" {
  description = "Private Pub/Sub-authenticated account-notification worker URL."
  value       = google_cloud_run_v2_service.notification.uri
}

output "image_service_url" {
  description = "Private Pub/Sub-authenticated capture-grouping worker URL."
  value       = google_cloud_run_v2_service.image.uri
}
