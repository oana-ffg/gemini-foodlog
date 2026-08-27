locals {
  production_cloud_run_services = [
    "foodlog-api",
    "foodlog-image",
    "foodlog-inference",
    "foodlog-mail-worker",
    "foodlog-notification",
  ]

  production_cloud_run_filter = join(" OR ", [
    for service in local.production_cloud_run_services :
    "resource.label.\"service_name\"=\"${service}\""
  ])

  consumer_subscriptions = [
    "foodlog-image-consumer",
    "foodlog-inference-consumer",
    "foodlog-mail-consumer",
    "foodlog-notification-consumer",
  ]

  dead_letter_subscriptions = [
    "foodlog-image-dead-letter-inspection",
    "foodlog-mail-dead-letter-inspection",
    "foodlog-notification-dead-letter-inspection",
  ]

  private_data_buckets = [
    "gemini-foodlog-2026-exports-163029863855",
    "gemini-foodlog-2026-media-163029863855",
    "gemini-foodlog-2026-raw-mail-163029863855",
    "gemini-foodlog-2026-traces-163029863855",
  ]

  consumer_subscription_filter = join(" OR ", [
    for subscription in local.consumer_subscriptions :
    "resource.label.\"subscription_id\"=\"${subscription}\""
  ])

  dead_letter_subscription_filter = join(" OR ", [
    for subscription in local.dead_letter_subscriptions :
    "resource.label.\"subscription_id\"=\"${subscription}\""
  ])

  private_data_bucket_filter = join(" OR ", [
    for bucket in local.private_data_buckets :
    "resource.label.\"bucket_name\"=\"${bucket}\""
  ])

  dashboard_charts = [
    {
      title     = "Stored-image rate"
      filter    = "metric.type=\"logging.googleapis.com/user/foodlog/stored_images\""
      aligner   = "ALIGN_RATE"
      reducer   = "REDUCE_SUM"
      group_by  = []
      axis      = "images / second"
      plot_type = "LINE"
    },
    {
      title     = "Analyzed-event rate"
      filter    = "metric.type=\"logging.googleapis.com/user/foodlog/analyzed_events\""
      aligner   = "ALIGN_RATE"
      reducer   = "REDUCE_SUM"
      group_by  = []
      axis      = "events / second"
      plot_type = "LINE"
    },
    {
      title     = "Gemini calls by workload and outcome"
      filter    = "metric.type=\"logging.googleapis.com/user/foodlog/model_calls\""
      aligner   = "ALIGN_RATE"
      reducer   = "REDUCE_SUM"
      group_by  = ["metric.label.\"workload\"", "metric.label.\"outcome\""]
      axis      = "calls / second"
      plot_type = "LINE"
    },
    {
      title     = "Gemini per-call cost p99"
      filter    = "metric.type=\"logging.googleapis.com/user/foodlog/model_call_cost_dkk_micros\""
      aligner   = "ALIGN_PERCENTILE_99"
      reducer   = "REDUCE_MAX"
      group_by  = ["metric.label.\"workload\"", "metric.label.\"outcome\""]
      axis      = "DKK micros"
      plot_type = "LINE"
    },
    {
      title     = "Processing failures by service"
      filter    = "metric.type=\"logging.googleapis.com/user/foodlog/processing_failures\""
      aligner   = "ALIGN_SUM"
      reducer   = "REDUCE_SUM"
      group_by  = ["metric.label.\"service\""]
      axis      = "failures"
      plot_type = "STACKED_BAR"
    },
    {
      title     = "Worker redeliveries by service"
      filter    = "metric.type=\"logging.googleapis.com/user/foodlog/worker_retries\""
      aligner   = "ALIGN_SUM"
      reducer   = "REDUCE_SUM"
      group_by  = ["metric.label.\"service\""]
      axis      = "redeliveries"
      plot_type = "STACKED_BAR"
    },
    {
      title     = "Observed public account capacity"
      filter    = "metric.type=\"logging.googleapis.com/user/foodlog/public_account_capacity_observed\""
      aligner   = "ALIGN_PERCENTILE_99"
      reducer   = "REDUCE_MAX"
      group_by  = []
      axis      = "active public accounts"
      plot_type = "LINE"
    },
    {
      title     = "Observed trial image usage"
      filter    = "metric.type=\"logging.googleapis.com/user/foodlog/trial_image_usage_observed\""
      aligner   = "ALIGN_PERCENTILE_99"
      reducer   = "REDUCE_MAX"
      group_by  = []
      axis      = "accepted images"
      plot_type = "LINE"
    },
    {
      title     = "Cloud Run requests by service and status class"
      filter    = "resource.type=\"cloud_run_revision\" AND metric.type=\"run.googleapis.com/request_count\" AND (${local.production_cloud_run_filter})"
      aligner   = "ALIGN_RATE"
      reducer   = "REDUCE_SUM"
      group_by  = ["resource.label.\"service_name\"", "metric.label.\"response_code_class\""]
      axis      = "requests / second"
      plot_type = "LINE"
    },
    {
      title     = "Cloud Run request latency p95"
      filter    = "resource.type=\"cloud_run_revision\" AND metric.type=\"run.googleapis.com/request_latencies\" AND (${local.production_cloud_run_filter})"
      aligner   = "ALIGN_PERCENTILE_95"
      reducer   = "REDUCE_MAX"
      group_by  = ["resource.label.\"service_name\""]
      axis      = "milliseconds"
      plot_type = "LINE"
    },
    {
      title     = "Cloud Run active instances"
      filter    = "resource.type=\"cloud_run_revision\" AND metric.type=\"run.googleapis.com/container/instance_count\" AND metric.label.\"state\"=\"active\" AND (${local.production_cloud_run_filter})"
      aligner   = "ALIGN_MAX"
      reducer   = "REDUCE_SUM"
      group_by  = ["resource.label.\"service_name\""]
      axis      = "instances"
      plot_type = "LINE"
    },
    {
      title     = "Pub/Sub consumer backlog"
      filter    = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" AND (${local.consumer_subscription_filter})"
      aligner   = "ALIGN_MAX"
      reducer   = "REDUCE_MAX"
      group_by  = ["resource.label.\"subscription_id\""]
      axis      = "messages"
      plot_type = "LINE"
    },
    {
      title     = "Pub/Sub oldest unacked message"
      filter    = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\" AND (${local.consumer_subscription_filter})"
      aligner   = "ALIGN_MAX"
      reducer   = "REDUCE_MAX"
      group_by  = ["resource.label.\"subscription_id\""]
      axis      = "seconds"
      plot_type = "LINE"
    },
    {
      title     = "Dead-letter inspection backlog"
      filter    = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" AND (${local.dead_letter_subscription_filter})"
      aligner   = "ALIGN_MAX"
      reducer   = "REDUCE_MAX"
      group_by  = ["resource.label.\"subscription_id\""]
      axis      = "dead-letter messages"
      plot_type = "STACKED_BAR"
    },
    {
      title     = "Private object storage"
      filter    = "resource.type=\"gcs_bucket\" AND metric.type=\"storage.googleapis.com/storage/total_bytes\" AND (${local.private_data_bucket_filter})"
      aligner   = "ALIGN_MAX"
      reducer   = "REDUCE_SUM"
      group_by  = ["resource.label.\"bucket_name\""]
      axis      = "bytes"
      plot_type = "LINE"
    },
  ]
}

resource "google_monitoring_dashboard" "production" {
  project = var.project_id

  dashboard_json = jsonencode({
    displayName = "Gemini FoodLog - Production operations"
    labels = {
      foodlog    = ""
      production = ""
    }
    mosaicLayout = {
      columns = 12
      tiles = concat(
        [
          {
            width  = 12
            height = 4
            widget = {
              title = "Ownership, budget, and response boundary"
              text = {
                format  = "MARKDOWN"
                style   = {}
                content = <<-EOT
                  ## Owner: Oana

                  Operational alerts go to the dedicated FoodLog email channel. The Google Cloud gross-spend budget is **DKK 400/month**, with alerts at **DKK 100, 200, and 300**. Credits do not hide gross-spend alerts. The promotion expires **24 September 2026**; current spend remains authoritative in [Cloud Billing](https://console.cloud.google.com/billing/010F14-E316E1-5D2C4F/budgets?project=gemini-foodlog-2026), not in Monitoring.

                  The Firestore model-spend ledger currently enforces the separate DKK 400 hard stop. A budget alert is not a kill switch. Follow the [credit-expiry runbook](https://github.com/oana-ffg/gemini-foodlog/blob/main/docs/credit-expiry-runbook.md) before the promotion expires. `telemetry_verifier` incidents are deliberate alert-delivery tests; other service failures require investigation.
                EOT
              }
            }
          }
        ],
        [
          for index, chart in local.dashboard_charts : merge(
            {
              yPos   = 4 + floor(index / 2) * 4
              width  = 6
              height = 4
              widget = {
                title = chart.title
                xyChart = {
                  chartOptions = {
                    mode = "COLOR"
                  }
                  dataSets = [
                    {
                      plotType   = chart.plot_type
                      targetAxis = "Y1"
                      timeSeriesQuery = {
                        timeSeriesFilter = {
                          filter = chart.filter
                          aggregation = merge(
                            {
                              alignmentPeriod    = "60s"
                              perSeriesAligner   = chart.aligner
                              crossSeriesReducer = chart.reducer
                            },
                            length(chart.group_by) == 0 ? {} : {
                              groupByFields = chart.group_by
                            }
                          )
                        }
                      }
                    }
                  ]
                  yAxis = {
                    label = chart.axis
                    scale = "LINEAR"
                  }
                }
              }
            },
            index % 2 == 0 ? {} : {
              xPos = 6
            }
          )
        ]
      )
    }
  })

  deletion_policy = "PREVENT"

  depends_on = [
    google_logging_metric.account_capacity,
    google_logging_metric.analyzed_events,
    google_logging_metric.model_call_cost,
    google_logging_metric.model_calls,
    google_logging_metric.processing_failures,
    google_logging_metric.stored_images,
    google_logging_metric.trial_image_usage,
    google_logging_metric.worker_retries,
  ]
}

output "production_monitoring_dashboard_id" {
  description = "Cloud Monitoring dashboard containing FoodLog production operations signals."
  value       = google_monitoring_dashboard.production.id
}
