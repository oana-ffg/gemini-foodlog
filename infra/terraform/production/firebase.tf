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

resource "google_firebase_web_app" "web" {
  provider = google-beta
  project  = var.project_id

  api_key_id      = google_apikeys_key.web.uid
  display_name    = "Gemini FoodLog Web"
  deletion_policy = "PREVENT"

  depends_on = [
    google_firebase_project.default,
  ]
}

resource "google_apikeys_key" "web" {
  # The Firebase-created key reports its owning project as the immutable
  # numeric project identifier. Use that same canonical value so importing the
  # existing key never proposes a replacement solely due to ID formatting.
  project = var.project_number

  name            = "cba476ef-f334-4efe-a703-6a6a5a33a7b4"
  display_name    = "Gemini FoodLog restricted web key"
  deletion_policy = "PREVENT"

  lifecycle {
    prevent_destroy = true
  }

  restrictions {
    browser_key_restrictions {
      allowed_referrers = [
        "http://127.0.0.1/*",
        "http://127.0.0.1:*/*",
        "http://localhost/*",
        "http://localhost:*/*",
        "https://gemini-foodlog-2026.firebaseapp.com/*",
        "https://gemini-foodlog-2026.web.app/*",
      ]
    }

    api_targets {
      service = "firebaseappcheck.googleapis.com"
    }

    api_targets {
      service = "firebaseinstallations.googleapis.com"
    }

    api_targets {
      service = "identitytoolkit.googleapis.com"
    }

    api_targets {
      service = "securetoken.googleapis.com"
    }
  }

  depends_on = [
    google_project_service.required["apikeys.googleapis.com"],
  ]
}

import {
  to = google_apikeys_key.web
  id = "projects/163029863855/locations/global/keys/cba476ef-f334-4efe-a703-6a6a5a33a7b4"
}

output "firebase_web_app_id" {
  description = "Opaque Firebase application identifier used by the web client."
  value       = google_firebase_web_app.web.app_id
}
