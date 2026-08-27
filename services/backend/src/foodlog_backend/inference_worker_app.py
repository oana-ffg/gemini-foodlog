from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from foodlog_agent.event_processing import (
    AdkEventReasoner,
    ClaimedEventInference,
    EventInferenceProcessor,
)

from .errors import CaptureNotFound
from .firestore_repository import FirestoreRepository
from .image_events import CaptureStoredEventV1
from .models import (
    ClarificationQuestion,
    JobKind,
    JobStatus,
    MealEntry,
    event_inference_job_id,
    utc_now,
)
from .operational_logging import emit_operational_event, install_request_logging, safe_error_kind
from .pattern_detection import PatternDetectionService
from .pubsub import PubSubPushEnvelope, decode_event
from .repository import Repository


class InferenceWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOODLOG_INFERENCE_")

    environment: Literal["test", "production"] = "production"
    gcp_project_id: str
    public_account_limit: int = Field(default=25, ge=1)
    trial_image_limit: int = Field(default=200, ge=1)
    model_spend_limit_dkk_micros: int = Field(default=400_000_000, ge=1)


class InferenceExecutor(Protocol):
    async def process(
        self,
        *,
        account_id: str,
        event_id: str,
        expected_revision: int,
        worker_id: str,
        invocation_key: str | None = None,
    ) -> ClaimedEventInference | None: ...

    async def publish(self, claimed: ClaimedEventInference) -> MealEntry | None: ...


class PatternDetector(Protocol):
    async def detect_and_propose(
        self,
        *,
        account_id: str,
        max_proposals: int = 2,
    ) -> list[ClarificationQuestion]: ...


def create_inference_worker_app(
    settings: InferenceWorkerSettings | None = None,
    *,
    repository: Repository | None = None,
    processor: InferenceExecutor | None = None,
    pattern_detector: PatternDetector | None = None,
) -> FastAPI:
    active_settings = settings or InferenceWorkerSettings()
    active_repository = repository or FirestoreRepository(
        project_id=active_settings.gcp_project_id,
        public_account_limit=active_settings.public_account_limit,
        trial_image_limit=active_settings.trial_image_limit,
        model_spend_limit_dkk_micros=(active_settings.model_spend_limit_dkk_micros),
    )
    active_processor = processor or EventInferenceProcessor(
        repository=active_repository,
        reasoner=AdkEventReasoner(),
    )
    pattern_detection = pattern_detector or PatternDetectionService(active_repository)
    app = FastAPI(
        title="Gemini FoodLog inference worker",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_request_logging(
        app,
        service="inference_worker",
        environment=active_settings.environment,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": active_settings.environment}

    @app.post("/internal/pubsub/capture-stored", status_code=status.HTTP_204_NO_CONTENT)
    async def infer_stored_capture(envelope: PubSubPushEnvelope) -> Response:
        try:
            event = decode_event(
                envelope,
                CaptureStoredEventV1,
                event_name="capture-stored",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid_pubsub_event") from error

        try:
            capture = await active_repository.capture_for_account(
                account_id=event.account_id,
                capture_id=event.capture_id,
            )
        except CaptureNotFound as error:
            raise HTTPException(status_code=503, detail="capture_not_ready") from error
        if capture.event_id is None:
            raise HTTPException(status_code=503, detail="capture_not_grouped")

        job_id = event_inference_job_id(capture.event_id)
        job = await active_repository.job_for_account(event.account_id, job_id)
        if job is None:
            raise HTTPException(status_code=503, detail="inference_job_not_ready")
        if job.kind != JobKind.EVENT_INFERENCE or job.subject_id != capture.event_id:
            emit_operational_event(
                "ERROR",
                "inference_job_identity_invalid",
                account_id=event.account_id,
                event_id=capture.event_id,
                job_id=job_id,
            )
            raise HTTPException(status_code=503, detail="invalid_inference_job")
        if job.status == JobStatus.COMPLETED:
            await pattern_detection.detect_and_propose(account_id=event.account_id)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        now = utc_now()
        if job.status == JobStatus.PENDING and job.available_at > now:
            raise HTTPException(status_code=503, detail="inference_quiet_period")
        if (
            job.status == JobStatus.LEASED
            and job.lease_expires_at is not None
            and job.lease_expires_at > now
        ):
            raise HTTPException(status_code=503, detail="inference_already_running")

        try:
            claimed = await active_processor.process(
                account_id=event.account_id,
                event_id=capture.event_id,
                expected_revision=job.subject_revision,
                worker_id=f"pubsub:{envelope.message.message_id}"[:128],
            )
            if claimed is not None:
                await active_processor.publish(claimed)
            await pattern_detection.detect_and_propose(account_id=event.account_id)
        except Exception as error:
            emit_operational_event(
                "ERROR",
                "event_inference_failed",
                account_id=event.account_id,
                event_id=capture.event_id,
                job_id=job_id,
                subject_revision=job.subject_revision,
                message_id=envelope.message.message_id,
                error_kind=safe_error_kind(error),
            )
            raise HTTPException(status_code=503, detail="event_inference_failed") from error

        latest = await active_repository.job_for_account(event.account_id, job_id)
        if latest is None or latest.status != JobStatus.COMPLETED:
            raise HTTPException(status_code=503, detail="inference_not_completed")
        emit_operational_event(
            "INFO",
            "event_inference_completed",
            account_id=event.account_id,
            event_id=capture.event_id,
            job_id=job_id,
            subject_revision=job.subject_revision,
            message_id=envelope.message.message_id,
            outcome="published",
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app
