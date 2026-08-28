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

variable "unlimited_owner_user_ids" {
  description = "Firebase UIDs explicitly exempted from public capacity and image-trial limits. Supply through ignored local Terraform input; never commit user identifiers."
  type        = set(string)
  default     = []
  sensitive   = true

  validation {
    condition = alltrue([
      for uid in var.unlimited_owner_user_ids :
      length(uid) >= 1 && length(uid) <= 128 && uid == trimspace(uid)
    ])
    error_message = "Unlimited Firebase UIDs must be non-empty, trimmed, and at most 128 characters."
  }
}

locals {
  model_spend_limit_dkk_micros = 400000000

  api_runtime_environment = {
    FOODLOG_ALLOWED_ORIGINS = jsonencode([
      "https://gemini-foodlog-2026.firebaseapp.com",
      "https://gemini-foodlog-2026.web.app",
    ])
    FOODLOG_AUTH_BACKEND                    = "firebase"
    FOODLOG_ENVIRONMENT                     = var.environment
    FOODLOG_EXPORT_RECENT_AUTH_SECONDS      = "300"
    FOODLOG_EXPORT_REQUEST_COOLDOWN_SECONDS = "3600"
    FOODLOG_FIREBASE_PROJECT_ID             = var.project_id
    FOODLOG_GCP_PROJECT_ID                  = var.project_id
    FOODLOG_GROUPING_POLICY_VERSION         = "temporal-v1"
    FOODLOG_GROUPING_QUIET_SECONDS          = "30"
    FOODLOG_GROUPING_REOPEN_SECONDS         = "7200"
    FOODLOG_IMAGE_TOPIC                     = google_pubsub_topic.events["image"].id
    FOODLOG_INBOUND_MAIL_DOMAIN             = "${var.project_id}.appspotmail.com"
    FOODLOG_LAUNCH_CONSENT_POLICY_VERSION   = "launch-interest-v1"
    FOODLOG_MEDIA_BUCKET                    = google_storage_bucket.retained["media"].name
    FOODLOG_MODEL_SPEND_LIMIT_DKK_MICROS    = tostring(local.model_spend_limit_dkk_micros)
    FOODLOG_NOTIFICATION_TOPIC              = google_pubsub_topic.events["notification"].id
    FOODLOG_PUBLIC_ACCOUNT_LIMIT            = "25"
    FOODLOG_STORAGE_BACKEND                 = "gcp"
    FOODLOG_TRIAL_IMAGE_LIMIT               = "200"
    FOODLOG_UNLIMITED_OWNER_USER_IDS        = jsonencode(sort(tolist(var.unlimited_owner_user_ids)))
    FOODLOG_WAITLIST_POLICY_VERSION         = "capacity-waitlist-v1"
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

  inference_runtime_environment = {
    FOODLOG_INFERENCE_ENVIRONMENT                  = var.environment
    FOODLOG_INFERENCE_GCP_PROJECT_ID               = var.project_id
    FOODLOG_INFERENCE_MODEL_SPEND_LIMIT_DKK_MICROS = tostring(local.model_spend_limit_dkk_micros)
    FOODLOG_INFERENCE_PUBLIC_ACCOUNT_LIMIT         = "25"
    FOODLOG_INFERENCE_TRIAL_IMAGE_LIMIT            = "200"
    FOODLOG_MEDIA_BUCKET                           = google_storage_bucket.retained["media"].name
    FOODLOG_MODEL                                  = "gemini-3.6-flash"
    FOODLOG_TRACE_BUCKET                           = google_storage_bucket.retained["traces"].name
    GOOGLE_CLOUD_LOCATION                          = "eu"
    GOOGLE_CLOUD_PROJECT                           = var.project_id
    GOOGLE_GENAI_USE_VERTEXAI                      = "true"
  }

  mail_worker_runtime_environment = {
    FOODLOG_MAIL_WORKER_ENVIRONMENT          = var.environment
    FOODLOG_MAIL_WORKER_GCP_PROJECT_ID       = var.project_id
    FOODLOG_MAIL_WORKER_PUBLIC_ACCOUNT_LIMIT = "25"
    FOODLOG_MAIL_WORKER_RAW_MAIL_BUCKET      = google_storage_bucket.retained["raw_mail"].name
    FOODLOG_MAIL_WORKER_TRIAL_IMAGE_LIMIT    = "200"
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

# Reusable, scale-to-zero security smoke. It makes no request unless explicitly
# executed and runs under the same keyless identity as the production agent.
resource "google_cloud_run_v2_job" "vertex_access_smoke" {
  project  = var.project_id
  name     = "foodlog-vertex-access-smoke"
  location = var.region

  deletion_protection = true

  labels = merge(local.common_labels, {
    component = "vertex-access-smoke"
  })

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account = google_service_account.runtime["worker"].email
      timeout         = "60s"
      max_retries     = 0

      containers {
        name    = "probe"
        image   = var.api_container_image
        command = ["python"]
        args = [
          "-m",
          "foodlog_backend.model_probe",
          "--project",
          var.project_id,
          "--location",
          "eu",
          "--model",
          "gemini-3.6-flash",
          "--confirm-billable-probe",
        ]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_iam_member.worker_vertex_model_invoker,
  ]
}

resource "google_cloud_run_v2_job" "adk_agent_smoke" {
  project  = var.project_id
  name     = "foodlog-adk-agent-smoke"
  location = var.region

  deletion_protection = true

  labels = merge(local.common_labels, {
    component = "adk-agent-smoke"
  })

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account = google_service_account.runtime["worker"].email
      timeout         = "120s"
      max_retries     = 0

      containers {
        name    = "agent-smoke"
        image   = var.api_container_image
        command = ["python"]
        args    = ["-m", "foodlog_agent.smoke"]

        env {
          name  = "FOODLOG_MODEL"
          value = "gemini-3.6-flash"
        }

        env {
          name  = "FOODLOG_MODEL_SPEND_LIMIT_DKK_MICROS"
          value = tostring(local.model_spend_limit_dkk_micros)
        }

        env {
          name  = "FOODLOG_MEDIA_BUCKET"
          value = google_storage_bucket.retained["media"].name
        }

        env {
          name  = "FOODLOG_TRACE_BUCKET"
          value = google_storage_bucket.retained["traces"].name
        }

        env {
          name  = "GOOGLE_CLOUD_LOCATION"
          value = "eu"
        }

        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }

        env {
          name  = "GOOGLE_GENAI_USE_VERTEXAI"
          value = "true"
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_iam_member.worker_vertex_model_invoker,
  ]
}

# Reusable, no-model smoke for the production Firestore/GCS event-evidence path.
# It has no default account target and fails safely unless an operator supplies
# an exact account/event pair plus the explicit private-read confirmation flag.
resource "google_cloud_run_v2_job" "event_evidence_smoke" {
  project  = var.project_id
  name     = "foodlog-event-evidence-smoke"
  location = var.region

  deletion_protection = true

  labels = merge(local.common_labels, {
    component = "event-evidence-smoke"
  })

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account = google_service_account.runtime["worker"].email
      timeout         = "120s"
      max_retries     = 0

      containers {
        name    = "event-evidence-smoke"
        image   = var.api_container_image
        command = ["python"]
        args    = ["-m", "foodlog_agent.event_evidence_smoke"]

        env {
          name  = "FOODLOG_MEDIA_BUCKET"
          value = google_storage_bucket.retained["media"].name
        }

        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_storage_bucket_iam_policy.retained["media"],
  ]
}

# Reusable, no-model smoke for tenant-scoped recent meals, active notes,
# unresolved review state, and bounded household-wiki selection/read. It has no
# default target and requires an explicit private-read confirmation per execution.
resource "google_cloud_run_v2_job" "context_tools_smoke" {
  project  = var.project_id
  name     = "foodlog-context-tools-smoke"
  location = var.region

  deletion_protection = true

  labels = merge(local.common_labels, {
    component = "context-tools-smoke"
  })

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account = google_service_account.runtime["worker"].email
      timeout         = "120s"
      max_retries     = 0

      containers {
        name    = "context-tools-smoke"
        image   = var.api_container_image
        command = ["python"]
        args    = ["-m", "foodlog_agent.context_tools_smoke"]

        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Isolated, no-model proof that the Firestore transaction rejects a reservation
# above a deliberately tiny ceiling before any provider invocation can occur.
resource "google_cloud_run_v2_job" "model_spend_smoke" {
  project  = var.project_id
  name     = "foodlog-model-spend-smoke"
  location = var.region

  deletion_protection = true

  labels = merge(local.common_labels, {
    component = "model-spend-smoke"
  })

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account = google_service_account.runtime["worker"].email
      timeout         = "60s"
      max_retries     = 0

      containers {
        name    = "model-spend-smoke"
        image   = var.api_container_image
        command = ["python"]
        args    = ["-m", "foodlog_backend.model_spend_smoke"]

        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_iam_member.datastore_runtime["worker"],
  ]
}

# Rerunnable production proof for account-scoped meal, question, feedback, and
# immutable-revision persistence. The job writes only a fixed internal fixture.
resource "google_cloud_run_v2_job" "firestore_repository_smoke" {
  project  = var.project_id
  name     = "foodlog-firestore-repository-smoke"
  location = var.region

  deletion_protection = true

  labels = merge(local.common_labels, {
    component = "repository-smoke"
  })

  template {
    parallelism = 1
    task_count  = 1

    template {
      service_account = google_service_account.runtime["worker"].email
      timeout         = "60s"
      max_retries     = 0

      containers {
        name    = "repository-smoke"
        image   = var.api_container_image
        command = ["python"]
        args = [
          "-m",
          "foodlog_backend.firestore_repository_smoke",
          "--confirm-isolated-smoke",
        ]

        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_iam_member.datastore_runtime["worker"],
  ]
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

resource "google_cloud_run_v2_service" "inference" {
  project  = var.project_id
  name     = "foodlog-inference"
  location = var.region

  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  labels = merge(local.common_labels, {
    component = "inference"
  })

  template {
    service_account                  = google_service_account.runtime["worker"].email
    timeout                          = "300s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      name    = "inference"
      image   = var.api_container_image
      command = ["uvicorn"]
      args = [
        "foodlog_backend.inference_worker_main:app",
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
          memory = "1Gi"
        }

        cpu_idle          = true
        startup_cpu_boost = false
      }

      dynamic "env" {
        for_each = local.inference_runtime_environment

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
    google_project_iam_member.worker_vertex_model_invoker,
    google_storage_bucket_iam_policy.retained["media"],
    google_storage_bucket_iam_policy.retained["traces"],
  ]
}

resource "google_cloud_run_v2_service_iam_member" "inference_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.inference.location
  name     = google_cloud_run_v2_service.inference.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runtime["worker"].email}"
}

resource "google_cloud_run_v2_service" "mail_worker" {
  project  = var.project_id
  name     = "foodlog-mail-worker"
  location = var.region

  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  labels = merge(local.common_labels, {
    component = "mail-worker"
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
      name    = "mail-worker"
      image   = var.api_container_image
      command = ["uvicorn"]
      args = [
        "foodlog_backend.mail_worker_main:app",
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
        for_each = local.mail_worker_runtime_environment

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
    google_storage_bucket_iam_policy.retained["raw_mail"],
  ]
}

# The same private worker identity that reads raw mail from GCS is the only
# principal allowed to invoke this internal service through Pub/Sub OIDC.
resource "google_cloud_run_v2_service_iam_member" "mail_worker_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.mail_worker.location
  name     = google_cloud_run_v2_service.mail_worker.name
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

output "inference_service_url" {
  description = "Private Pub/Sub-authenticated Gemini event-inference worker URL."
  value       = google_cloud_run_v2_service.inference.uri
}

output "mail_worker_service_url" {
  description = "Private Pub/Sub-authenticated purchase-mail classifier URL."
  value       = google_cloud_run_v2_service.mail_worker.uri
}
