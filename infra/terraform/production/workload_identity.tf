locals {
  github_owner_id           = "212630009"
  github_repository_id      = "1343967496"
  github_production_subject = "repo:oana-ffg@${local.github_owner_id}/gemini-foodlog@${local.github_repository_id}:environment:production"

  github_ci_service_accounts = toset([
    "ci_deploy",
    "ci_infra",
  ])
}

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions"
  display_name              = "GitHub Actions"
  description               = "Keyless CI trust for the exact FoodLog GitHub repository."
  disabled                  = false
  deletion_policy           = "PREVENT"

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["iam.googleapis.com"],
    google_project_service.required["sts.googleapis.com"],
  ]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "foodlog"
  display_name                       = "FoodLog GitHub production"
  description                        = "Accepts only immutable FoodLog production-environment tokens from main."
  disabled                           = false
  deletion_policy                    = "PREVENT"

  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.environment"         = "assertion.environment"
    "attribute.ref"                 = "assertion.ref"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
  }

  attribute_condition = join(" && ", [
    "assertion.repository_id == '${local.github_repository_id}'",
    "assertion.repository_owner_id == '${local.github_owner_id}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.environment == 'production'",
    "assertion.sub == '${local.github_production_subject}'",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com/"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account_iam_member" "github_ci_federation" {
  for_each = local.github_ci_service_accounts

  service_account_id = google_service_account.runtime[each.value].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${local.github_repository_id}"
}

output "github_workload_identity_provider" {
  description = "Provider resource name used by keyless GitHub Actions authentication."
  value       = google_iam_workload_identity_pool_provider.github.name
}
