from datetime import timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .firestore_repository import FirestoreRepository
from .grouping import CaptureGroupingService, GroupingPolicy
from .image_events import CaptureStoredEventV1
from .pubsub import PubSubPushEnvelope, decode_event
from .repository import Repository


class ImageWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOODLOG_IMAGE_")

    environment: Literal["test", "production"] = "production"
    gcp_project_id: str
    grouping_policy_version: str = Field(default="temporal-v1", min_length=1, max_length=80)
    grouping_quiet_seconds: int = Field(default=30, ge=1, le=3_600)
    grouping_reopen_seconds: int = Field(default=7_200, ge=1, le=86_400)
    public_account_limit: int = Field(default=25, ge=1)
    trial_image_limit: int = Field(default=200, ge=1)

    def grouping_policy(self) -> GroupingPolicy:
        return GroupingPolicy(
            version=self.grouping_policy_version,
            quiet_after=timedelta(seconds=self.grouping_quiet_seconds),
            reopen_window=timedelta(seconds=self.grouping_reopen_seconds),
        )


def create_image_worker_app(
    settings: ImageWorkerSettings | None = None,
    *,
    repository: Repository | None = None,
) -> FastAPI:
    active_settings = settings or ImageWorkerSettings()
    active_repository = repository or FirestoreRepository(
        project_id=active_settings.gcp_project_id,
        public_account_limit=active_settings.public_account_limit,
        trial_image_limit=active_settings.trial_image_limit,
    )
    service = CaptureGroupingService(
        repository=active_repository,
        policy=active_settings.grouping_policy(),
    )
    app = FastAPI(
        title="Gemini FoodLog image worker",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": active_settings.environment}

    @app.post("/internal/pubsub/capture-stored", status_code=status.HTTP_204_NO_CONTENT)
    async def group_stored_capture(envelope: PubSubPushEnvelope) -> Response:
        try:
            event = decode_event(
                envelope,
                CaptureStoredEventV1,
                event_name="capture-stored",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid_pubsub_event") from error
        try:
            await service.process(
                account_id=event.account_id,
                capture_id=event.capture_id,
                worker_id=f"pubsub:{envelope.message.message_id}"[:200],
            )
        except Exception as error:
            raise HTTPException(status_code=503, detail="capture_grouping_failed") from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
