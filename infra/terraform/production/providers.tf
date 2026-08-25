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

provider "google" {
  project = var.project_id
  region  = var.region
}
