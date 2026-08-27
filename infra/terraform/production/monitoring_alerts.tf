locals {
  monitoring_alerts = {
    processing_failure = {
      display_name   = "FoodLog production processing failure"
      condition_name = "Any redacted application failure"
      filter         = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/foodlog/processing_failures\""
      comparison     = "COMPARISON_GT"
      threshold      = 0
      duration       = "0s"
      aligner        = "ALIGN_SUM"
      reducer        = "REDUCE_SUM"
      group_by       = ["metric.label.\"service\""]
      severity       = "ERROR"
      subject        = "FoodLog processing failure requires triage"
      documentation  = "Open the Gemini FoodLog production dashboard and filter structured logs by the affected service. A `telemetry_verifier` incident is the deliberate delivery test; every other service is product work. Inspect the correlated request, event, capture, message, or job ID without downloading private payloads."
    }
    dead_letter_backlog = {
      display_name   = "FoodLog dead-letter backlog"
      condition_name = "Any retained dead-letter message"
      filter         = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" AND (${local.dead_letter_subscription_filter})"
      comparison     = "COMPARISON_GT"
      threshold      = 0
      duration       = "0s"
      aligner        = "ALIGN_MAX"
      reducer        = "REDUCE_MAX"
      group_by       = ["resource.label.\"subscription_id\""]
      severity       = "CRITICAL"
      subject        = "FoodLog has retained dead-letter work"
      documentation  = "Do not acknowledge or republish the message blindly. Inspect the named `foodlog-*-dead-letter-inspection` subscription, correlate the immutable event and job state, then use the explicit replay workflow once OPS-005 is complete."
    }
    queue_age = {
      display_name   = "FoodLog worker queue is stale"
      condition_name = "Oldest consumer message exceeds ten minutes"
      filter         = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\" AND (${local.consumer_subscription_filter})"
      comparison     = "COMPARISON_GT"
      threshold      = 600
      duration       = "300s"
      aligner        = "ALIGN_MAX"
      reducer        = "REDUCE_MAX"
      group_by       = ["resource.label.\"subscription_id\""]
      severity       = "ERROR"
      subject        = "FoodLog worker queue has been stale for five minutes"
      documentation  = "Check the named subscription backlog, push response codes, worker revision health, and model-spend hard-stop state. Preserve messages; do not purge the subscription."
    }
    cloud_run_5xx = {
      display_name   = "FoodLog Cloud Run 5xx responses"
      condition_name = "Any sustained production 5xx"
      filter         = "resource.type=\"cloud_run_revision\" AND metric.type=\"run.googleapis.com/request_count\" AND metric.label.\"response_code_class\"=\"5xx\" AND resource.label.\"service_name\"=\"foodlog-api\""
      comparison     = "COMPARISON_GT"
      threshold      = 0
      duration       = "60s"
      aligner        = "ALIGN_SUM"
      reducer        = "REDUCE_SUM"
      group_by       = ["resource.label.\"service_name\""]
      severity       = "ERROR"
      subject        = "FoodLog production service is returning 5xx"
      documentation  = "Open the production dashboard and correlate API request IDs with structured failures. Worker 503 responses are intentionally excluded because they can represent normal Pub/Sub redelivery; worker health is covered by processing-failure, stale-queue, retry, and dead-letter signals."
    }
    expensive_model_call = {
      display_name   = "FoodLog unusually expensive Gemini call"
      condition_name = "Production model call exceeds DKK 5"
      filter         = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/foodlog/model_call_cost_dkk_micros\" AND metric.label.\"workload\"=\"production\""
      comparison     = "COMPARISON_GT"
      threshold      = 5000000
      duration       = "0s"
      aligner        = "ALIGN_PERCENTILE_99"
      reducer        = "REDUCE_MAX"
      group_by       = ["metric.label.\"outcome\""]
      severity       = "WARNING"
      subject        = "FoodLog recorded a Gemini call above DKK 5"
      documentation  = "Compare the usage record with the Firestore model-spend ledger and retained trace metadata. The ledger, not this alert, owns the cumulative hard stop. Check prompt growth, tool loops, retry count, and whether the call was billed despite failure."
    }
    account_capacity = {
      display_name   = "FoodLog public account capacity nearing limit"
      condition_name = "Observed public account count is at least 23"
      filter         = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/foodlog/public_account_capacity_observed\""
      comparison     = "COMPARISON_GT"
      threshold      = 22
      duration       = "0s"
      aligner        = "ALIGN_PERCENTILE_99"
      reducer        = "REDUCE_MAX"
      group_by       = []
      severity       = "WARNING"
      subject        = "FoodLog has two or fewer public account slots left"
      documentation  = "Verify `system/public_capacity` in Firestore and the 25-account signup gate. Do not raise the limit without rechecking credits, expected traffic, and Oana's explicit amount decision. The waitlist remains available when capacity is full."
    }
    trial_image_capacity = {
      display_name   = "FoodLog trial image quota nearing limit"
      condition_name = "Observed trial usage is at least 190 images"
      filter         = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/foodlog/trial_image_usage_observed\""
      comparison     = "COMPARISON_GT"
      threshold      = 189
      duration       = "0s"
      aligner        = "ALIGN_PERCENTILE_99"
      reducer        = "REDUCE_MAX"
      group_by       = []
      severity       = "WARNING"
      subject        = "A FoodLog trial is within ten images of exhaustion"
      documentation  = "Use the source log around the metric point to identify the account correlation field, then verify its entitlement record. Do not silently extend the 200-image trial or expose the account ID as a metric label."
    }
  }
}

resource "google_monitoring_notification_channel" "oana_email" {
  project      = var.project_id
  display_name = "Oana - Gemini FoodLog operations"
  description  = "Owned operational alerts for the private Gemini FoodLog hackathon prototype."
  type         = "email"
  enabled      = true
  force_delete = false

  labels = {
    email_address = "oanagoge@gmail.com"
  }

  user_labels = {
    application = "gemini-foodlog"
    environment = var.environment
    owner       = "oana"
  }

  deletion_policy = "PREVENT"
}

resource "google_monitoring_alert_policy" "metric" {
  for_each = local.monitoring_alerts

  project               = var.project_id
  display_name          = each.value.display_name
  combiner              = "OR"
  enabled               = true
  severity              = each.value.severity
  notification_channels = [google_monitoring_notification_channel.oana_email.name]
  deletion_policy       = "PREVENT"

  conditions {
    display_name = each.value.condition_name

    condition_threshold {
      filter                  = each.value.filter
      comparison              = each.value.comparison
      threshold_value         = each.value.threshold
      duration                = each.value.duration
      evaluation_missing_data = each.value.duration == "0s" ? null : "EVALUATION_MISSING_DATA_INACTIVE"

      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = each.value.aligner
        cross_series_reducer = each.value.reducer
        group_by_fields      = each.value.group_by
      }

      trigger {
        count = 1
      }
    }
  }

  documentation {
    subject   = each.value.subject
    content   = each.value.documentation
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close           = "1800s"
    notification_prompts = ["OPENED", "CLOSED"]
  }

  user_labels = {
    application = "gemini-foodlog"
    environment = var.environment
    owner       = "oana"
  }

  depends_on = [
    google_logging_metric.account_capacity,
    google_logging_metric.model_call_cost,
    google_logging_metric.processing_failures,
    google_logging_metric.trial_image_usage,
  ]
}

output "monitoring_notification_channel_name" {
  description = "Notification channel that owns FoodLog operational alert delivery."
  value       = google_monitoring_notification_channel.oana_email.name
}

output "monitoring_alert_policy_names" {
  description = "Enabled FoodLog production alert policies."
  value       = { for key, policy in google_monitoring_alert_policy.metric : key => policy.name }
}
