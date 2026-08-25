from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .firestore_repository import FirestoreRepository
from .notifications import (
    AccountCreatedEventV1,
    AccountNotificationService,
    PushoverClient,
    PushoverSender,
)
from .pubsub import PubSubPushEnvelope, decode_event
from .repository import Repository


class NotificationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOODLOG_NOTIFICATION_")

    environment: Literal["test", "production"] = "production"
    gcp_project_id: str
    pushover_app_token: str = Field(min_length=30)
    pushover_user_key: str = Field(min_length=30)
    public_account_limit: int = Field(default=25, ge=1)
    trial_image_limit: int = Field(default=200, ge=1)


def create_notification_app(
    settings: NotificationSettings | None = None,
    *,
    repository: Repository | None = None,
    sender: PushoverSender | None = None,
) -> FastAPI:
    active_settings = settings or NotificationSettings()
    active_repository = repository or FirestoreRepository(
        project_id=active_settings.gcp_project_id,
        public_account_limit=active_settings.public_account_limit,
        trial_image_limit=active_settings.trial_image_limit,
    )
    active_sender = sender or PushoverClient(
        app_token=active_settings.pushover_app_token,
        user_key=active_settings.pushover_user_key,
    )
    service = AccountNotificationService(
        repository=active_repository,
        sender=active_sender,
    )
    app = FastAPI(
        title="Gemini FoodLog notification worker",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": active_settings.environment}

    @app.post("/internal/pubsub/account-created", status_code=status.HTTP_204_NO_CONTENT)
    async def deliver_account_created(envelope: PubSubPushEnvelope) -> Response:
        try:
            event = decode_event(
                envelope,
                AccountCreatedEventV1,
                event_name="account-created",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid_pubsub_event") from error
        try:
            await service.deliver(event.event_id)
        except Exception as error:
            raise HTTPException(status_code=503, detail="notification_delivery_failed") from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
