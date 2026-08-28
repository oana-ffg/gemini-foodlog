from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .account_export_events import AccountExportRequestedEventV1
from .account_exports import AccountExportService, AccountExportSnapshotReader
from .firestore_export_snapshot import FirestoreAccountExportSnapshotReader
from .firestore_repository import FirestoreRepository
from .operational_logging import emit_operational_event, install_request_logging, safe_error_kind
from .pubsub import PubSubPushEnvelope, decode_event
from .repository import Repository
from .storage import GCSObjectStore, ObjectStore


class ExportWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOODLOG_EXPORT_WORKER_")

    environment: Literal["test", "production"] = "production"
    gcp_project_id: str
    media_bucket: str = Field(min_length=3)
    raw_mail_bucket: str = Field(min_length=3)
    trace_bucket: str = Field(min_length=3)
    export_bucket: str = Field(min_length=3)
    public_account_limit: int = Field(default=25, ge=1)
    trial_image_limit: int = Field(default=200, ge=1)


def create_export_worker_app(
    settings: ExportWorkerSettings | None = None,
    *,
    repository: Repository | None = None,
    snapshot_reader: AccountExportSnapshotReader | None = None,
    source_stores: dict[str, ObjectStore] | None = None,
    export_store: ObjectStore | None = None,
) -> FastAPI:
    active_settings = settings or ExportWorkerSettings()
    active_repository = repository or FirestoreRepository(
        project_id=active_settings.gcp_project_id,
        public_account_limit=active_settings.public_account_limit,
        trial_image_limit=active_settings.trial_image_limit,
    )
    if snapshot_reader is None:
        if not isinstance(active_repository, FirestoreRepository):
            raise ValueError("a non-Firestore export worker requires a snapshot reader")
        snapshot_reader = FirestoreAccountExportSnapshotReader(active_repository.client)
    if source_stores is None:
        source_stores = {
            "media": GCSObjectStore(
                project_id=active_settings.gcp_project_id,
                bucket_name=active_settings.media_bucket,
            ),
            "raw_mail": GCSObjectStore(
                project_id=active_settings.gcp_project_id,
                bucket_name=active_settings.raw_mail_bucket,
            ),
            "traces": GCSObjectStore(
                project_id=active_settings.gcp_project_id,
                bucket_name=active_settings.trace_bucket,
            ),
        }
    active_export_store = export_store or GCSObjectStore(
        project_id=active_settings.gcp_project_id,
        bucket_name=active_settings.export_bucket,
    )
    service = AccountExportService(
        repository=active_repository,
        snapshot_reader=snapshot_reader,
        source_stores=source_stores,
        export_store=active_export_store,
    )
    app = FastAPI(
        title="Gemini FoodLog account export worker",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_request_logging(
        app,
        service="export_worker",
        environment=active_settings.environment,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": active_settings.environment}

    @app.post(
        "/internal/pubsub/account-export-requested",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def generate_account_export(envelope: PubSubPushEnvelope) -> Response:
        try:
            event = decode_event(
                envelope,
                AccountExportRequestedEventV1,
                event_name="account-export-requested",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid_pubsub_event") from error
        try:
            result = await service.process(
                account_id=event.account_id,
                export_id=event.export_id,
                worker_id=f"pubsub:{envelope.message.message_id}"[:128],
                delivery_attempt=envelope.delivery_attempt,
            )
        except Exception as error:
            emit_operational_event(
                "ERROR",
                "account_export_generation_failed",
                account_id=event.account_id,
                export_id=event.export_id,
                message_id=envelope.message.message_id,
                service="export_worker",
                delivery_attempt=envelope.delivery_attempt,
                error_kind=safe_error_kind(error),
            )
            raise HTTPException(
                status_code=503,
                detail="account_export_generation_failed",
            ) from error
        emit_operational_event(
            "INFO",
            "account_export_generation_completed",
            account_id=event.account_id,
            export_id=event.export_id,
            message_id=envelope.message.message_id,
            service="export_worker",
            delivery_attempt=envelope.delivery_attempt,
            outcome="completed" if result is not None else "already_processed",
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
