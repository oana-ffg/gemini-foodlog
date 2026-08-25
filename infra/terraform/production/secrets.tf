locals {
  notification_secrets = {
    pushover_app_token = {
      secret_id = "foodlog-pushover-app-token"
      purpose   = "pushover-application-token"
    }
    pushover_user_key = {
      secret_id = "foodlog-pushover-user-key"
      purpose   = "pushover-recipient-key"
    }
  }
}

# Terraform owns only secret metadata and IAM. Payload versions are loaded from
# gopass directly into Secret Manager so sensitive values never enter source,
# command arguments, environment files, Terraform plans, or Terraform state.
resource "google_secret_manager_secret" "notification" {
  for_each = local.notification_secrets

  project             = var.project_id
  secret_id           = each.value.secret_id
  deletion_protection = true

  labels = merge(local.common_labels, {
    component = "notification"
    purpose   = each.value.purpose
  })

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["secretmanager.googleapis.com"],
  ]
}

resource "google_secret_manager_secret_iam_member" "notification_accessor" {
  for_each = google_secret_manager_secret.notification

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime["notification"].email}"
}

output "notification_secret_ids" {
  description = "Secret Manager IDs consumed only by the notification worker."
  value = {
    for key, secret in google_secret_manager_secret.notification : key => secret.secret_id
  }
}
