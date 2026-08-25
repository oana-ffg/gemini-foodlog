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
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
  ])
}

provider "google" {
  project = var.project_id
  region  = var.region
}
