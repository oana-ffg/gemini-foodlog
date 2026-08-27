locals {
  operator_audit_log_types = {
    "logging.googleapis.com" = toset(["DATA_READ"])
    "storage.googleapis.com" = toset(["DATA_READ", "DATA_WRITE"])
  }

  operator_audit_runtime_exemptions = [
    for account in sort(tolist(local.datastore_runtime_accounts)) :
    "serviceAccount:${google_service_account.runtime[account].email}"
  ]
}

# Data Access audit logs are disabled by default. Enable the configurable APIs
# traversed by the local operator diagnostic, while exempting production identities.
# This preserves the actual ADC/CI principal for exceptional reads without turning
# routine API and worker traffic into an unbounded paid audit-log stream.
resource "google_project_iam_audit_config" "operator_data_access" {
  for_each = local.operator_audit_log_types

  project = var.project_id
  service = each.key

  dynamic "audit_log_config" {
    for_each = each.value

    content {
      log_type         = audit_log_config.value
      exempted_members = local.operator_audit_runtime_exemptions
    }
  }

  depends_on = [
    google_project_service.required,
  ]
}
