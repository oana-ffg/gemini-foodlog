variable "project_id" {
  description = "Google Cloud project that owns the FoodLog production resources."
  type        = string
  default     = "gemini-foodlog-2026"
}

variable "region" {
  description = "Primary Google Cloud region for regional FoodLog services."
  type        = string
  default     = "europe-west1"
}

variable "firestore_location" {
  description = "Multi-region used by Firestore and other location-coupled services."
  type        = string
  default     = "eur3"
}

variable "storage_location" {
  description = "Multi-region used by durable private user-data buckets."
  type        = string
  default     = "EU"
}

variable "environment" {
  description = "Deployment environment represented by this Terraform root."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["production"], var.environment)
    error_message = "This Terraform root represents production only."
  }
}

locals {
  common_labels = {
    application = "gemini-foodlog"
    environment = var.environment
    managed_by  = "terraform"
  }

  required_services = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "appengine.googleapis.com",
    "apikeys.googleapis.com",
    "cloudbuild.googleapis.com",
    "billingbudgets.googleapis.com",
    "firestore.googleapis.com",
    "firebase.googleapis.com",
    "firebaseappcheck.googleapis.com",
    "firebasehosting.googleapis.com",
    "firebaseinstallations.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "identitytoolkit.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "policytroubleshooter.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "securetoken.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
  ])
}

provider "google" {
  project               = var.project_id
  region                = var.region
  billing_project       = var.project_id
  user_project_override = true
}

provider "google-beta" {
  project               = var.project_id
  region                = var.region
  billing_project       = var.project_id
  user_project_override = true
}
