resource "google_firebase_project" "default" {
  provider = google-beta
  project  = var.project_id

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    google_project_service.required["firebase.googleapis.com"],
  ]
}

resource "google_identity_platform_config" "default" {
  project = var.project_id

  authorized_domains = [
    "localhost",
    "gemini-foodlog-2026.firebaseapp.com",
    "gemini-foodlog-2026.web.app",
  ]

  autodelete_anonymous_users = true

  sign_in {
    allow_duplicate_emails = false

    anonymous {
      enabled = false
    }

    email {
      enabled           = true
      password_required = true
    }

    phone_number {
      enabled = false
    }
  }

  depends_on = [
    google_firebase_project.default,
    google_project_service.required["identitytoolkit.googleapis.com"],
  ]
}
