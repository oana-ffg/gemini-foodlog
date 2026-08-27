import logging
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .firestore_repository import FirestoreRepository
from .mail_events import RawMailStoredEventV1
from .pubsub import PubSubPushEnvelope, decode_event
from .purchase_mail import (
    MailClassificationOutcome,
    classify_nemlig_purchase_email,
    raw_mail_object_key,
)
from .purchase_normalization import parse_purchase_document
from .repository import Repository
from .storage import GCSObjectStore, ObjectStore

LOGGER = logging.getLogger(__name__)


class MailWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOODLOG_MAIL_WORKER_")

    environment: Literal["test", "production"] = "production"
    gcp_project_id: str
    raw_mail_bucket: str = Field(min_length=3)
    public_account_limit: int = Field(default=25, ge=1)
    trial_image_limit: int = Field(default=200, ge=1)


def create_mail_worker_app(
    settings: MailWorkerSettings | None = None,
    *,
    repository: Repository | None = None,
    object_store: ObjectStore | None = None,
) -> FastAPI:
    active_settings = settings or MailWorkerSettings()
    active_repository = repository or FirestoreRepository(
        project_id=active_settings.gcp_project_id,
        public_account_limit=active_settings.public_account_limit,
        trial_image_limit=active_settings.trial_image_limit,
    )
    active_store = object_store or GCSObjectStore(
        project_id=active_settings.gcp_project_id,
        bucket_name=active_settings.raw_mail_bucket,
    )
    app = FastAPI(
        title="Gemini FoodLog mail worker",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": active_settings.environment}

    @app.post("/internal/pubsub/raw-mail-stored", status_code=status.HTTP_204_NO_CONTENT)
    async def classify_raw_mail(envelope: PubSubPushEnvelope) -> Response:
        try:
            event = decode_event(envelope, RawMailStoredEventV1, event_name="raw-mail-stored")
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid_pubsub_event") from error
        try:
            key = raw_mail_object_key(account_id=event.account_id, mail_id=event.mail_id)
            raw_message = await active_store.get(event.account_id, key)
            classification = classify_nemlig_purchase_email(
                raw_message,
                account_id=event.account_id,
                mail_id=event.mail_id,
            )
            if classification.outcome == MailClassificationOutcome.PURCHASE_DOCUMENT:
                assert classification.candidate is not None
                identity = await active_repository.attach_purchase_document(
                    classification.candidate
                )
                parsed = parse_purchase_document(
                    raw_message,
                    kind=identity.document.kind,
                )
                await active_repository.normalize_purchase_document(
                    document=identity.document,
                    parsed=parsed,
                )
        except Exception as error:
            LOGGER.exception(
                "Purchase-mail classification failed for account %s mail %s",
                event.account_id,
                event.mail_id,
            )
            raise HTTPException(status_code=503, detail="mail_classification_failed") from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
