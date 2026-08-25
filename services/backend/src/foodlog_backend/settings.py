from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOODLOG_", env_file=".env")

    environment: Literal["local", "test", "preview", "production"] = "local"
    auth_backend: Literal["local", "firebase"] = "local"
    storage_backend: Literal["memory", "gcp"] = "memory"
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:5173"])
    trial_image_limit: int = Field(default=200, ge=1)
    public_account_limit: int = Field(default=25, ge=1)
    unlimited_owner_user_ids: set[str] = Field(default_factory=set)
    preview_shared_secret: str | None = Field(default=None, min_length=32)
    gcp_project_id: str | None = None
    firebase_project_id: str | None = None
    media_bucket: str | None = None

    @model_validator(mode="after")
    def reject_memory_in_production(self) -> "Settings":
        if self.environment == "preview" and self.preview_shared_secret is None:
            raise ValueError("Preview requires a shared secret in addition to Cloud Run IAM")
        if self.environment == "preview" and self.auth_backend != "local":
            raise ValueError("Preview uses its explicit local shared-secret authentication")
        if self.environment == "production" and self.storage_backend == "memory":
            raise ValueError("Production cannot start with the in-memory storage adapter")
        if self.storage_backend == "gcp" and (
            self.gcp_project_id is None or self.media_bucket is None
        ):
            raise ValueError("GCP storage requires gcp_project_id and media_bucket")
        if self.environment == "production" and self.auth_backend != "firebase":
            raise ValueError("Production requires Firebase authentication")
        if self.auth_backend == "firebase" and self.firebase_project_id is None:
            raise ValueError("Firebase authentication requires firebase_project_id")
        return self
