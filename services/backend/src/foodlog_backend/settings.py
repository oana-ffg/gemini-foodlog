from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .inbound_mail import normalize_inbound_mail_domain


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOODLOG_", env_file=".env")

    environment: Literal["local", "test", "preview", "production"] = "local"
    auth_backend: Literal["local", "firebase"] = "local"
    storage_backend: Literal["memory", "gcp"] = "memory"
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:5173"])
    trial_image_limit: int = Field(default=200, ge=1)
    public_account_limit: int = Field(default=25, ge=1)
    unlimited_owner_user_ids: set[str] = Field(default_factory=set)
    launch_consent_policy_version: str = Field(default="launch-interest-v1", min_length=1)
    waitlist_policy_version: str = Field(default="capacity-waitlist-v1", min_length=1)
    grouping_policy_version: str = Field(default="temporal-v1", min_length=1, max_length=80)
    grouping_quiet_seconds: int = Field(default=30, ge=1, le=3_600)
    grouping_reopen_seconds: int = Field(default=7_200, ge=1, le=86_400)
    preview_shared_secret: str | None = Field(default=None, min_length=32)
    gcp_project_id: str | None = None
    firebase_project_id: str | None = None
    media_bucket: str | None = None
    image_topic: str | None = None
    notification_topic: str | None = None
    inbound_mail_domain: str = Field(default="foodlog.invalid", min_length=3, max_length=253)

    @field_validator("inbound_mail_domain")
    @classmethod
    def normalize_mail_domain(cls, value: str) -> str:
        return normalize_inbound_mail_domain(value)

    @model_validator(mode="after")
    def reject_memory_in_production(self) -> "Settings":
        if self.grouping_reopen_seconds < self.grouping_quiet_seconds:
            raise ValueError(
                "grouping_reopen_seconds must not be shorter than grouping_quiet_seconds"
            )
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
        if self.environment == "production" and self.notification_topic is None:
            raise ValueError("Production requires the account notification topic")
        if self.environment == "production" and self.image_topic is None:
            raise ValueError("Production requires the capture image topic")
        expected_mail_domain = (
            f"{self.gcp_project_id}.appspotmail.com" if self.gcp_project_id else None
        )
        if self.environment == "production" and self.inbound_mail_domain != expected_mail_domain:
            raise ValueError(
                "Production inbound_mail_domain must match the project's App Engine mail domain"
            )
        if self.auth_backend == "firebase" and self.firebase_project_id is None:
            raise ValueError("Firebase authentication requires firebase_project_id")
        return self
