# FoodLog operational telemetry

FoodLog uses native Google Cloud metrics wherever the platform already emits the
signal and a deliberately small set of log-based metrics for application facts.
This keeps the prototype observable without creating per-account or per-capture
time series.

## Signal map

| Operator question | Source |
| --- | --- |
| How many new images were stored? | `logging.googleapis.com/user/foodlog/stored_images` |
| How many kitchen events were analyzed? | `logging.googleapis.com/user/foodlog/analyzed_events` |
| How many Gemini calls ran, and how did they end? | `logging.googleapis.com/user/foodlog/model_calls` |
| What did individual Gemini calls cost? | `logging.googleapis.com/user/foodlog/model_call_cost_dkk_micros`; the Firestore `system/model_spend` ledger is authoritative for the cumulative hard stop |
| Which application services are failing? | `logging.googleapis.com/user/foodlog/processing_failures` |
| Which workers are being redelivered? | `logging.googleapis.com/user/foodlog/worker_retries` |
| How full are the 25 public slots and 200-image trials? | `logging.googleapis.com/user/foodlog/public_account_capacity_observed` and `logging.googleapis.com/user/foodlog/trial_image_usage_observed` |
| Is a queue backing up? | `pubsub.googleapis.com/subscription/num_undelivered_messages` and `pubsub.googleapis.com/subscription/oldest_unacked_message_age` on each consumer subscription |
| Did work reach a dead-letter topic? | `pubsub.googleapis.com/subscription/num_undelivered_messages` on each `foodlog-*-dead-letter-inspection` subscription |
| How much private object data exists? | `storage.googleapis.com/storage/total_bytes` and `storage.googleapis.com/storage/object_count` for the media, mail, trace, and export buckets |
| Are services scaling or slowing down? | `run.googleapis.com/request_count`, `run.googleapis.com/request_latencies`, and `run.googleapis.com/container/instance_count` |

The application records account, capture, event, and message IDs in redacted
operational logs for correlation, but none of those identifiers becomes a metric
label. Model labels are limited to configured model, purpose, production/evaluation
workload, and succeeded/failed outcome. Worker metrics use only the fixed service
name. This bounds active time-series cardinality even as user evidence grows.

## Cost boundary

Log-based metrics are chargeable Cloud Monitoring custom metrics. Google currently
includes the first 150 MiB of chargeable metric ingestion per billing account each
month. The eight descriptors here receive at most one point per relevant request or
worker outcome and expose no unbounded labels. They are still covered by the DKK 400
gross-spend budget and must be revisited before materially raising account, camera,
or capture limits.

Application operational logs remain in the normal Cloud Logging retention path.
Full model requests, responses, and tool traces stay in the private trace bucket and
are never copied into these metrics.

Operator diagnostics additionally enable Data Access audit records for Storage
reads/writes and Cloud Logging reads. The four routine data-plane service accounts
are exempt, so normal API, camera, and worker activity cannot turn this into an
unbounded audit stream. Exceptional operator and deployment access is retained with
Google's authenticated principal and remains variable log ingestion under the same
DKK 400 gross-spend budget; it is not treated as a free signal. Firestore Native
does not accept a service-level Data Access audit config, so the diagnostic writes a
scoped immutable application audit event before any Firestore evidence read.

## Verification contract

Metric filters are tested against emitted JSON records before deployment. After
deployment, verify each descriptor exists, emit a uniquely timestamped safe event,
query the matching log entry, and then query its Monitoring time series after the
documented ingestion delay. A metric point is not proof of durable product state:
compare stored-image and analyzed-event samples with the corresponding private GCS
object and Firestore event/job evidence. Compare model-cost samples with the
Firestore spend ledger, which owns the actual cumulative kill-switch total.
