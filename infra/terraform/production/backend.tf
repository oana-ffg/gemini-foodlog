terraform {
  backend "gcs" {
    bucket = "gemini-foodlog-2026-tfstate-163029863855"
    prefix = "production"
  }
}
