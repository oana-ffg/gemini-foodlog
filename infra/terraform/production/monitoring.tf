locals {
  operational_event_filter = "jsonPayload.schema=\"foodlog_operational_event_v1\""
}

resource "google_logging_metric" "stored_images" {
  project         = var.project_id
  name            = "foodlog/stored_images"
  description     = "New immutable FoodLog capture objects completed by the API; idempotent duplicate requests are excluded."
  filter          = "${local.operational_event_filter} AND jsonPayload.event=\"capture_storage_completed\" AND jsonPayload.outcome=\"stored\""
  deletion_policy = "PREVENT"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "FoodLog stored images"
  }

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

resource "google_logging_metric" "analyzed_events" {
  project         = var.project_id
  name            = "foodlog/analyzed_events"
  description     = "FoodLog kitchen events whose inference revision was successfully published."
  filter          = "${local.operational_event_filter} AND jsonPayload.event=\"event_inference_completed\""
  deletion_policy = "PREVENT"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "FoodLog analyzed events"
  }

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

resource "google_logging_metric" "model_calls" {
  project         = var.project_id
  name            = "foodlog/model_calls"
  description     = "Reconciled Gemini calls, partitioned only by bounded model, purpose, workload, and outcome dimensions."
  filter          = "${local.operational_event_filter} AND jsonPayload.event=\"model_usage_recorded\""
  deletion_policy = "PREVENT"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "FoodLog model calls"

    labels {
      key         = "model"
      value_type  = "STRING"
      description = "Configured Gemini model identifier."
    }

    labels {
      key         = "purpose"
      value_type  = "STRING"
      description = "Bounded application model-call purpose."
    }

    labels {
      key         = "workload"
      value_type  = "STRING"
      description = "Production or evaluation workload."
    }

    labels {
      key         = "outcome"
      value_type  = "STRING"
      description = "Succeeded or failed model-call outcome."
    }
  }

  label_extractors = {
    model    = "EXTRACT(jsonPayload.model)"
    purpose  = "EXTRACT(jsonPayload.purpose)"
    workload = "EXTRACT(jsonPayload.workload)"
    outcome  = "EXTRACT(jsonPayload.outcome)"
  }

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

resource "google_logging_metric" "model_call_cost" {
  project         = var.project_id
  name            = "foodlog/model_call_cost_dkk_micros"
  description     = "Conservatively priced DKK micros for each reconciled Gemini call. The Firestore spend ledger remains the authoritative cumulative hard-stop value."
  filter          = "${local.operational_event_filter} AND jsonPayload.event=\"model_usage_recorded\""
  value_extractor = "EXTRACT(jsonPayload.actual_dkk_micros)"
  deletion_policy = "PREVENT"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "1"
    display_name = "FoodLog model call cost in DKK micros"

    labels {
      key         = "workload"
      value_type  = "STRING"
      description = "Production or evaluation workload."
    }

    labels {
      key         = "outcome"
      value_type  = "STRING"
      description = "Succeeded or failed model-call outcome."
    }
  }

  label_extractors = {
    workload = "EXTRACT(jsonPayload.workload)"
    outcome  = "EXTRACT(jsonPayload.outcome)"
  }

  bucket_options {
    explicit_buckets {
      bounds = [1, 100, 1000, 10000, 100000, 1000000, 10000000]
    }
  }

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

resource "google_logging_metric" "processing_failures" {
  project         = var.project_id
  name            = "foodlog/processing_failures"
  description     = "Redacted FoodLog operational failures, partitioned only by bounded service name."
  filter          = "${local.operational_event_filter} AND severity>=ERROR"
  deletion_policy = "PREVENT"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "FoodLog processing failures"

    labels {
      key         = "service"
      value_type  = "STRING"
      description = "Bounded FoodLog API or worker service name."
    }
  }

  label_extractors = {
    service = "EXTRACT(jsonPayload.service)"
  }

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

resource "google_logging_metric" "worker_retries" {
  project         = var.project_id
  name            = "foodlog/worker_retries"
  description     = "Successful or failed Pub/Sub push deliveries whose platform delivery attempt is greater than one."
  filter          = "${local.operational_event_filter} AND jsonPayload.delivery_attempt>1"
  deletion_policy = "PREVENT"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "INT64"
    unit         = "1"
    display_name = "FoodLog worker retries"

    labels {
      key         = "service"
      value_type  = "STRING"
      description = "Bounded FoodLog worker service name."
    }
  }

  label_extractors = {
    service = "EXTRACT(jsonPayload.service)"
  }

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

resource "google_logging_metric" "account_capacity" {
  project         = var.project_id
  name            = "foodlog/public_account_capacity_observed"
  description     = "Public account slot observed when a trial account is first created. With no MVP account deletion, the maximum observation is the active public-account count."
  filter          = "${local.operational_event_filter} AND jsonPayload.event=\"account_capacity_observed\""
  value_extractor = "EXTRACT(jsonPayload.account_capacity_count)"
  deletion_policy = "PREVENT"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "1"
    display_name = "FoodLog observed public account capacity"
  }

  bucket_options {
    linear_buckets {
      num_finite_buckets = 26
      width              = 1
      offset             = 0
    }
  }

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

resource "google_logging_metric" "trial_image_usage" {
  project         = var.project_id
  name            = "foodlog/trial_image_usage_observed"
  description     = "Accepted trial-image count observed after successful capture storage; account identity is deliberately not a metric label."
  filter          = "${local.operational_event_filter} AND jsonPayload.event=\"capture_storage_completed\" AND jsonPayload.trial_image_limit:*"
  value_extractor = "EXTRACT(jsonPayload.accepted_image_count)"
  deletion_policy = "PREVENT"

  metric_descriptor {
    metric_kind  = "DELTA"
    value_type   = "DISTRIBUTION"
    unit         = "1"
    display_name = "FoodLog observed trial image usage"
  }

  bucket_options {
    explicit_buckets {
      bounds = [1, 10, 25, 50, 100, 150, 175, 190, 200]
    }
  }

  depends_on = [google_project_service.required["logging.googleapis.com"]]
}

output "foodlog_log_metric_types" {
  description = "Bounded application log-based metrics available to dashboards and alerts."
  value = {
    analyzed_events   = "logging.googleapis.com/user/${google_logging_metric.analyzed_events.name}"
    account_capacity  = "logging.googleapis.com/user/${google_logging_metric.account_capacity.name}"
    model_call_cost   = "logging.googleapis.com/user/${google_logging_metric.model_call_cost.name}"
    model_calls       = "logging.googleapis.com/user/${google_logging_metric.model_calls.name}"
    failures          = "logging.googleapis.com/user/${google_logging_metric.processing_failures.name}"
    stored_images     = "logging.googleapis.com/user/${google_logging_metric.stored_images.name}"
    trial_image_usage = "logging.googleapis.com/user/${google_logging_metric.trial_image_usage.name}"
    worker_retries    = "logging.googleapis.com/user/${google_logging_metric.worker_retries.name}"
  }
}
