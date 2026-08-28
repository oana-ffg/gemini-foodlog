from hashlib import sha256
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .firestore_repository import FirestoreRepository
from .mail_authentication import DkimMailAuthenticator, MailAuthenticator
from .mail_events import RawMailStoredEventV1
from .operational_logging import emit_operational_event, install_request_logging, safe_error_kind
from .pubsub import PubSubPushEnvelope, decode_event
from .purchase_mail import (
    MailClassificationOutcome,
    classify_nemlig_purchase_email,
    raw_mail_object_key,
)
from .purchase_normalization import parse_purchase_document
from .repository import Repository
from .storage import GCSObjectStore, ObjectStore


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
    mail_authenticator: MailAuthenticator | None = None,
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
    active_authenticator = mail_authenticator or DkimMailAuthenticator()
    app = FastAPI(
        title="Gemini FoodLog mail worker",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_request_logging(app, service="mail_worker", environment=active_settings.environment)

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
            raw_content_sha256 = sha256(raw_message).hexdigest()
            authentication = await active_repository.raw_mail_authentication(
                account_id=event.account_id,
                raw_mail_id=event.mail_id,
            )
            if authentication is None:
                authentication = await active_authenticator.authenticate(
                    raw_message,
                    account_id=event.account_id,
                    raw_mail_id=event.mail_id,
                )
                authentication = await active_repository.record_raw_mail_authentication(
                    authentication
                )
            if authentication.raw_content_sha256 != raw_content_sha256:
                raise ValueError("raw-mail authentication content hash mismatch")
            classification = classify_nemlig_purchase_email(
                raw_message,
                account_id=event.account_id,
                mail_id=event.mail_id,
                authentication=authentication,
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
            emit_operational_event(
                "ERROR",
                "purchase_mail_processing_failed",
                account_id=event.account_id,
                mail_id=event.mail_id,
                message_id=envelope.message.message_id,
                service="mail_worker",
                delivery_attempt=envelope.delivery_attempt,
                error_kind=safe_error_kind(error),
            )
            raise HTTPException(status_code=503, detail="mail_classification_failed") from error
        emit_operational_event(
            "INFO",
            "purchase_mail_processing_completed",
            account_id=event.account_id,
            mail_id=event.mail_id,
            message_id=envelope.message.message_id,
            service="mail_worker",
            delivery_attempt=envelope.delivery_attempt,
            outcome=classification.outcome.value,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
