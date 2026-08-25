from dataclasses import dataclass
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .auth import (
    FirebaseIdentityTokenVerifier,
    IdentityTokenVerifier,
    InvalidAuthenticationToken,
    VerifiedIdentity,
)
from .errors import (
    AccountAlreadyProvisioned,
    AccountCapacityReached,
    AccountNotProvisioned,
    CameraNotFound,
    CaptureNotFound,
    CrossAccountAccess,
    IdempotencyConflict,
    MealNotFound,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    TrialQuotaExhausted,
    WaitlistUnavailable,
)
from .firestore_repository import FirestoreRepository
from .inference import FixtureInferenceEngine, InferenceEngine
from .models import (
    Account,
    BrowserCamera,
    BrowserCameraCreate,
    CaptureAccepted,
    ClarificationQuestion,
    LaunchMailConsent,
    LaunchMailConsentRequest,
    MealEntry,
    MealFeedbackRequest,
    MealFeedbackResult,
    MealRevision,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionStatus,
    WaitlistEntry,
    WaitlistJoinRequest,
)
from .repository import InMemoryRepository, Repository
from .service import CaptureService
from .settings import Settings
from .storage import GCSObjectStore, InMemoryObjectStore, ObjectStore

MAX_IMAGE_BYTES = 5 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png"}


def detected_image_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


@dataclass
class Container:
    repository: Repository
    object_store: ObjectStore
    capture_service: CaptureService


def create_app(
    settings: Settings | None = None,
    *,
    inference_engine: InferenceEngine | None = None,
    token_verifier: IdentityTokenVerifier | None = None,
) -> FastAPI:
    active_settings = settings or Settings()
    if active_settings.environment == "production" and (
        inference_engine is None or isinstance(inference_engine, FixtureInferenceEngine)
    ):
        raise ValueError(
            "Production requires an explicitly configured non-fixture inference engine"
        )
    if active_settings.auth_backend == "firebase":
        assert active_settings.firebase_project_id is not None
        active_token_verifier = token_verifier or FirebaseIdentityTokenVerifier(
            active_settings.firebase_project_id
        )
    else:
        if token_verifier is not None:
            raise ValueError("A token verifier cannot be configured with local authentication")
        active_token_verifier = None
    if active_settings.storage_backend == "gcp":
        assert active_settings.gcp_project_id is not None
        assert active_settings.media_bucket is not None
        repository: Repository = FirestoreRepository(
            project_id=active_settings.gcp_project_id,
            public_account_limit=active_settings.public_account_limit,
            trial_image_limit=active_settings.trial_image_limit,
            unlimited_owner_user_ids=active_settings.unlimited_owner_user_ids,
        )
        object_store: ObjectStore = GCSObjectStore(
            project_id=active_settings.gcp_project_id,
            bucket_name=active_settings.media_bucket,
        )
    else:
        repository = InMemoryRepository(
            public_account_limit=active_settings.public_account_limit,
            trial_image_limit=active_settings.trial_image_limit,
            unlimited_owner_user_ids=active_settings.unlimited_owner_user_ids,
        )
        object_store = InMemoryObjectStore()
    container = Container(
        repository=repository,
        object_store=object_store,
        capture_service=CaptureService(
            repository=repository,
            object_store=object_store,
            inference=inference_engine or FixtureInferenceEngine(),
        ),
    )
    app = FastAPI(title="Gemini FoodLog API", version="0.1.0")
    app.state.container = container
    allowed_headers = ["Content-Type", "Idempotency-Key"]
    if active_settings.auth_backend == "firebase":
        allowed_headers.append("Authorization")
    else:
        allowed_headers.extend(
            ["X-FoodLog-Local-User", "X-FoodLog-Preview-Secret"]
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=allowed_headers,
    )

    async def request_identity(
        authorization: str | None = Header(default=None),
        x_foodlog_local_user: str | None = Header(default=None),
        x_foodlog_preview_secret: str | None = Header(default=None),
    ) -> VerifiedIdentity:
        if active_settings.auth_backend == "firebase":
            parts = authorization.split() if authorization else []
            if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > 8192:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="A valid bearer token is required",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            assert active_token_verifier is not None
            try:
                identity = await active_token_verifier.verify(parts[1])
            except InvalidAuthenticationToken as error:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="A valid bearer token is required",
                    headers={"WWW-Authenticate": "Bearer"},
                ) from error
            if not identity.email_verified:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="email_verification_required",
                )
            return identity

        if active_settings.environment == "preview" and (
            x_foodlog_preview_secret is None
            or active_settings.preview_shared_secret is None
            or not compare_digest(
                x_foodlog_preview_secret,
                active_settings.preview_shared_secret,
            )
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        if not x_foodlog_local_user or len(x_foodlog_local_user) > 128:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Local user header is required",
            )
        return VerifiedIdentity(uid=x_foodlog_local_user, email_verified=True)

    async def request_user_id(
        identity: Annotated[VerifiedIdentity, Depends(request_identity)],
    ) -> str:
        return identity.uid

    def verified_email(identity: VerifiedIdentity) -> str:
        if identity.email is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="verified_email_required",
            )
        return identity.email

    @app.exception_handler(AccountCapacityReached)
    async def account_capacity_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"signup_capacity_exhausted"}',
            media_type="application/json",
        )

    @app.exception_handler(AccountAlreadyProvisioned)
    async def account_already_provisioned_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"account_already_provisioned"}',
            media_type="application/json",
        )

    @app.exception_handler(WaitlistUnavailable)
    async def waitlist_unavailable_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"signup_capacity_available"}',
            media_type="application/json",
        )

    @app.exception_handler(TrialQuotaExhausted)
    async def trial_quota_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content='{"detail":"trial_image_quota_exhausted"}',
            media_type="application/json",
        )

    @app.exception_handler(AccountNotProvisioned)
    async def account_missing_handler(*_: object) -> Response:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    async def resource_missing_handler(*_: object) -> Response:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    app.add_exception_handler(CameraNotFound, resource_missing_handler)
    app.add_exception_handler(CaptureNotFound, resource_missing_handler)
    app.add_exception_handler(MealNotFound, resource_missing_handler)
    app.add_exception_handler(QuestionNotFound, resource_missing_handler)

    @app.exception_handler(CrossAccountAccess)
    async def cross_account_handler(*_: object) -> Response:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"idempotency_key_reused_with_different_capture"}',
            media_type="application/json",
        )

    @app.exception_handler(QuestionAlreadyAnswered)
    async def question_already_answered_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"question_already_answered"}',
            media_type="application/json",
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": active_settings.environment}

    @app.post("/v1/accounts", response_model=Account)
    async def provision_account(user_id: str = Depends(request_user_id)) -> Account:
        return await container.repository.provision_account(user_id)

    @app.post("/v1/consents/launch-mail", response_model=LaunchMailConsent)
    async def record_launch_mail_consent(
        request: LaunchMailConsentRequest,
        identity: Annotated[VerifiedIdentity, Depends(request_identity)],
    ) -> LaunchMailConsent:
        return await container.repository.record_launch_mail_consent(
            owner_user_id=identity.uid,
            email_normalized=verified_email(identity),
            granted=request.granted,
            policy_version=active_settings.launch_consent_policy_version,
        )

    @app.post("/v1/waitlist", response_model=WaitlistEntry)
    async def join_waitlist(
        request: WaitlistJoinRequest,
        identity: Annotated[VerifiedIdentity, Depends(request_identity)],
    ) -> WaitlistEntry:
        return await container.repository.join_waitlist(
            firebase_uid=identity.uid,
            email_normalized=verified_email(identity),
            policy_version=active_settings.waitlist_policy_version,
        )

    @app.post("/v1/browser-cameras", response_model=BrowserCamera)
    async def create_browser_camera(
        request: BrowserCameraCreate,
        user_id: str = Depends(request_user_id),
    ) -> BrowserCamera:
        return await container.repository.create_browser_camera(user_id, request.name)

    @app.post(
        "/v1/browser-cameras/{camera_id}/captures",
        response_model=CaptureAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def upload_browser_capture(
        camera_id: str,
        image: UploadFile,
        idempotency_key: str = Header(min_length=8, max_length=128),
        user_id: str = Depends(request_user_id),
    ) -> CaptureAccepted:
        if image.content_type not in SUPPORTED_IMAGE_TYPES:
            raise HTTPException(status_code=415, detail="Only JPEG and PNG images are accepted")
        content = await image.read(MAX_IMAGE_BYTES + 1)
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Image is empty or exceeds 5 MiB")
        if detected_image_type(content) != image.content_type:
            raise HTTPException(status_code=415, detail="Declared and actual image types differ")
        return await container.capture_service.accept_browser_capture(
            owner_user_id=user_id,
            camera_id=camera_id,
            idempotency_key=idempotency_key,
            content_type=image.content_type,
            image=content,
        )

    @app.get("/v1/journal", response_model=list[MealEntry])
    async def list_journal(user_id: str = Depends(request_user_id)) -> list[MealEntry]:
        return await container.repository.list_meals(user_id)

    @app.get("/v1/meals/{meal_id}/revisions", response_model=list[MealRevision])
    async def list_meal_revisions(
        meal_id: str,
        user_id: str = Depends(request_user_id),
    ) -> list[MealRevision]:
        return await container.repository.list_meal_revisions(user_id, meal_id)

    @app.post(
        "/v1/meals/{meal_id}/feedback",
        response_model=MealFeedbackResult,
    )
    async def record_meal_feedback(
        meal_id: str,
        request: MealFeedbackRequest,
        idempotency_key: str = Header(min_length=8, max_length=128),
        user_id: str = Depends(request_user_id),
    ) -> MealFeedbackResult:
        return await container.repository.record_meal_feedback(
            owner_user_id=user_id,
            meal_id=meal_id,
            request=request,
            idempotency_key=idempotency_key,
        )

    @app.get("/v1/questions", response_model=list[ClarificationQuestion])
    async def list_questions(
        question_status: QuestionStatus | None = QuestionStatus.OPEN,
        user_id: str = Depends(request_user_id),
    ) -> list[ClarificationQuestion]:
        return await container.repository.list_questions(
            user_id,
            question_status=question_status,
        )

    @app.post(
        "/v1/questions/{question_id}/answer",
        response_model=QuestionAnswerResult,
    )
    async def answer_question(
        question_id: str,
        request: QuestionAnswerRequest,
        idempotency_key: str = Header(min_length=8, max_length=128),
        user_id: str = Depends(request_user_id),
    ) -> QuestionAnswerResult:
        return await container.repository.answer_question(
            owner_user_id=user_id,
            question_id=question_id,
            request=request,
            idempotency_key=idempotency_key,
        )

    @app.get("/v1/captures/{capture_id}/image")
    async def get_capture_image(
        capture_id: str,
        user_id: str = Depends(request_user_id),
    ) -> Response:
        capture = await container.repository.capture_for_owner(user_id, capture_id)
        content = await container.object_store.get(capture.object_key)
        return Response(
            content=content,
            media_type=capture.content_type,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app
