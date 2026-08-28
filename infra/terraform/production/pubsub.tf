locals {
  pubsub_streams = {
    export = {
      publisher_account = "api"
      consumer_account  = "worker"
      purpose           = "account-export-generation"
    }
    image = {
      publisher_account = "api"
      consumer_account  = "worker"
      purpose           = "capture-processing"
    }
    mail = {
      publisher_account = "mail"
      consumer_account  = "worker"
      purpose           = "inbound-mail-processing"
    }
    notification = {
      publisher_account = "api"
      consumer_account  = "notification"
      purpose           = "account-notifications"
    }
  }

  pubsub_persistence_regions = [
    "europe-north1",
    "europe-west1",
    "europe-west4",
  ]

  pubsub_push_targets = {
    export = {
      endpoint              = "${google_cloud_run_v2_service.export_worker.uri}/internal/pubsub/account-export-requested"
      service_account_email = google_service_account.runtime["worker"].email
      audience              = google_cloud_run_v2_service.export_worker.uri
    }
    image = {
      endpoint              = "${google_cloud_run_v2_service.image.uri}/internal/pubsub/capture-stored"
      service_account_email = google_service_account.runtime["worker"].email
      audience              = google_cloud_run_v2_service.image.uri
    }
    mail = {
      endpoint              = "${google_cloud_run_v2_service.mail_worker.uri}/internal/pubsub/raw-mail-stored"
      service_account_email = google_service_account.runtime["worker"].email
      audience              = google_cloud_run_v2_service.mail_worker.uri
    }
    notification = {
      endpoint              = "${google_cloud_run_v2_service.notification.uri}/internal/pubsub/account-created"
      service_account_email = google_service_account.runtime["notification"].email
      audience              = google_cloud_run_v2_service.notification.uri
    }
  }
}

resource "google_project_service_identity" "pubsub" {
  provider = google-beta
  project  = var.project_id
  service  = "pubsub.googleapis.com"

  depends_on = [
    google_project_service.required["pubsub.googleapis.com"],
  ]
}

resource "google_pubsub_topic" "events" {
  for_each = local.pubsub_streams

  project                    = var.project_id
  name                       = "foodlog-${each.key}-events"
  message_retention_duration = "604800s"
  deletion_policy            = "PREVENT"

  labels = merge(local.common_labels, {
    purpose = each.value.purpose
  })

  message_storage_policy {
    allowed_persistence_regions = local.pubsub_persistence_regions
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["pubsub.googleapis.com"],
  ]
}

resource "google_pubsub_topic" "dead_letter" {
  for_each = local.pubsub_streams

  project                    = var.project_id
  name                       = "foodlog-${each.key}-dead-letter"
  message_retention_duration = "1209600s"
  deletion_policy            = "PREVENT"

  labels = merge(local.common_labels, {
    purpose = "${each.value.purpose}-dead-letter"
  })

  message_storage_policy {
    allowed_persistence_regions = local.pubsub_persistence_regions
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["pubsub.googleapis.com"],
  ]
}

resource "google_pubsub_subscription" "consumer" {
  for_each = local.pubsub_streams

  project                    = var.project_id
  name                       = "foodlog-${each.key}-consumer"
  topic                      = google_pubsub_topic.events[each.key].id
  ack_deadline_seconds       = 600
  message_retention_duration = "604800s"
  retain_acked_messages      = false
  deletion_policy            = "PREVENT"

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter[each.key].id
    max_delivery_attempts = 5
  }

  dynamic "push_config" {
    for_each = contains(keys(local.pubsub_push_targets), each.key) ? [
      local.pubsub_push_targets[each.key]
    ] : []

    content {
      push_endpoint = push_config.value.endpoint

      oidc_token {
        service_account_email = push_config.value.service_account_email
        audience              = push_config.value.audience
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Every immutable capture event is independently delivered to the inference
# worker. This fan-out is the durable handoff: a grouping retry or service crash
# cannot strand an event between Firestore and a second best-effort publication.
resource "google_pubsub_subscription" "inference_consumer" {
  project                    = var.project_id
  name                       = "foodlog-inference-consumer"
  topic                      = google_pubsub_topic.events["image"].id
  ack_deadline_seconds       = 600
  message_retention_duration = "604800s"
  retain_acked_messages      = false
  deletion_policy            = "PREVENT"

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "30s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter["image"].id
    max_delivery_attempts = 20
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.inference.uri}/internal/pubsub/capture-stored"

    oidc_token {
      service_account_email = google_service_account.runtime["worker"].email
      audience              = google_cloud_run_v2_service.inference.uri
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# A subscription is required on every dead-letter topic so forwarded messages are
# retained for explicit inspection and replay rather than discarded on publication.
resource "google_pubsub_subscription" "dead_letter_inspection" {
  for_each = local.pubsub_streams

  project                    = var.project_id
  name                       = "foodlog-${each.key}-dead-letter-inspection"
  topic                      = google_pubsub_topic.dead_letter[each.key].id
  ack_deadline_seconds       = 60
  message_retention_duration = "1209600s"
  retain_acked_messages      = false
  deletion_policy            = "PREVENT"

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "30s"
    maximum_backoff = "600s"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_pubsub_topic_iam_member" "runtime_publisher" {
  for_each = local.pubsub_streams

  project = var.project_id
  topic   = google_pubsub_topic.events[each.key].name
  role    = "roles/pubsub.publisher"
  member = (
    "serviceAccount:${google_service_account.runtime[each.value.publisher_account].email}"
  )
}

# Pub/Sub's service agent republishes exhausted source messages to the DLQ and
# must be able to acknowledge them on the source subscription.
resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  for_each = local.pubsub_streams

  project = var.project_id
  topic   = google_pubsub_topic.dead_letter[each.key].name
  role    = "roles/pubsub.publisher"
  member  = google_project_service_identity.pubsub.member
}

resource "google_pubsub_subscription_iam_member" "dead_letter_forwarder" {
  for_each = local.pubsub_streams

  project      = var.project_id
  subscription = google_pubsub_subscription.consumer[each.key].name
  role         = "roles/pubsub.subscriber"
  member       = google_project_service_identity.pubsub.member
}

resource "google_pubsub_subscription_iam_member" "inference_dead_letter_forwarder" {
  project      = var.project_id
  subscription = google_pubsub_subscription.inference_consumer.name
  role         = "roles/pubsub.subscriber"
  member       = google_project_service_identity.pubsub.member
}

resource "google_pubsub_subscription_iam_member" "runtime_consumer" {
  for_each = local.pubsub_streams

  project      = var.project_id
  subscription = google_pubsub_subscription.consumer[each.key].name
  role         = "roles/pubsub.subscriber"
  member = (
    "serviceAccount:${google_service_account.runtime[each.value.consumer_account].email}"
  )
}

resource "google_pubsub_subscription_iam_member" "inference_runtime_consumer" {
  project      = var.project_id
  subscription = google_pubsub_subscription.inference_consumer.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.runtime["worker"].email}"
}

output "pubsub_topic_ids" {
  description = "Private event and dead-letter topic IDs used by application publishers."
  value = {
    for key in keys(local.pubsub_streams) : key => {
      events      = google_pubsub_topic.events[key].id
      dead_letter = google_pubsub_topic.dead_letter[key].id
    }
  }
}

output "pubsub_subscription_ids" {
  description = "Consumer and inspection subscriptions; push delivery is attached by INF-017."
  value = {
    for key in keys(local.pubsub_streams) : key => {
      consumer               = google_pubsub_subscription.consumer[key].id
      dead_letter_inspection = google_pubsub_subscription.dead_letter_inspection[key].id
    }
  }
}

output "inference_subscription_id" {
  description = "Durable stored-capture fan-out subscription for event inference."
  value       = google_pubsub_subscription.inference_consumer.id
}
