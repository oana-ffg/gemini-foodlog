import warnings
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from secrets import compare_digest, token_urlsafe
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image
from pydantic import ValidationError

from .audit import record_audit_event
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
    DeviceCredentialCollision,
    IdempotencyConflict,
    InboundAddressGenerationFailed,
    InvalidDeviceCredential,
    InvalidKnowledgeProvenance,
    InvalidKnowledgeTransition,
    InvalidMealCorrectionTarget,
    InvalidMealFeedbackTransition,
    KnowledgePageNotFound,
    KnowledgeRevisionConflict,
    MealNotFound,
    MealRevisionConflict,
    PurchaseNotFound,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    QuestionSuperseded,
    TrialQuotaExhausted,
    UserContextNoteNotFound,
    WaitlistEntryNotFound,
    WaitlistUnavailable,
)
from .feedback_learning import FeedbackLearningService, MealFeedbackLearningResult
from .firestore_repository import FirestoreRepository
from .household_teaching import HouseholdTeachingService
from .image_events import (
    CaptureEventPublisher,
    InMemoryCaptureEventPublisher,
    PubSubCaptureEventPublisher,
)
from .inbound_mail import InboundMailAddressService
from .models import (
    Account,
    AuditAction,
    AuditActorKind,
    AuditEvent,
    AuditSource,
    BrowserCamera,
    BrowserCameraCreate,
    Camera,
    CaptureAccepted,
    CaptureEnvelopeV1,
    ClarificationQuestion,
    ConsentPreferences,
    DeviceCamera,
    DeviceCameraCreate,
    DeviceCameraCredentialIssue,
    DeviceSession,
    InboundMailAddress,
    KnowledgePage,
    KnowledgePageHistory,
    KnowledgeRevisionResult,
    LaunchMailConsent,
    LaunchMailConsentRequest,
    MealEntry,
    MealFeedbackRequest,
    MealRevision,
    MealStatus,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionKind,
    QuestionResponseRequest,
    QuestionResponseResult,
    QuestionStatus,
    StableKnowledgeCorrectionCreate,
    StableKnowledgeRetirementCreate,
    StableKnowledgeTeachingCreate,
    StableKnowledgeTeachingResult,
    UserContextNote,
    UserContextNoteCreate,
    VerifiedDeviceIdentity,
    WaitlistEntry,
    WaitlistJoinRequest,
    capture_grouping_job_id,
    event_inference_job_id,
    utc_now,
)
from .notifications import (
    AccountProvisioningService,
    InMemoryNotificationPublisher,
    NotificationPublisher,
    PubSubNotificationPublisher,
)
from .pattern_hypotheses import PatternHypothesisService
from .processing_views import CaptureProcessingView, capture_processing_view
from .purchase_views import (
    PurchaseDetailView,
    PurchaseSummaryView,
    purchase_detail_view,
    purchase_summary_view,
)
from .repository import InMemoryRepository, Repository
from .service import CaptureService
from .settings import Settings
from .storage import GCSObjectStore, InMemoryObjectStore, ObjectStore

MAX_IMAGE_BYTES = 5 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png"}
IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG"}
MAX_IMAGE_DIMENSION = 4_096
MAX_CAPTURE_FUTURE_SKEW = timedelta(minutes=5)
DEVICE_TOKEN_PREFIX = "flc_v1_"
DEVICE_TOKEN_VERSION = 1


def detected_image_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def image_dimensions(content: bytes, content_type: str) -> tuple[int, int] | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as decoded:
                if decoded.format != IMAGE_FORMATS.get(content_type):
                    return None
                width, height = decoded.size
                if not (1 <= width <= MAX_IMAGE_DIMENSION):
                    return None
                if not (1 <= height <= MAX_IMAGE_DIMENSION):
                    return None
                decoded.load()
                return width, height
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
    ):
        return None


async def validated_image_content(image: UploadFile) -> tuple[bytes, str]:
    if image.content_type not in SUPPORTED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Only JPEG and PNG images are accepted")
    content = await image.read(MAX_IMAGE_BYTES + 1)
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image is empty or exceeds 5 MiB")
    if detected_image_type(content) != image.content_type:
        raise HTTPException(status_code=415, detail="Declared and actual image types differ")
    return content, image.content_type


@dataclass
class Container:
    repository: Repository
    object_store: ObjectStore
    capture_service: CaptureService
    account_service: AccountProvisioningService
    notification_publisher: NotificationPublisher
    capture_event_publisher: CaptureEventPublisher
    inbound_mail_address_service: InboundMailAddressService
    feedback_learning_service: FeedbackLearningService
    household_teaching_service: HouseholdTeachingService
    pattern_hypothesis_service: PatternHypothesisService


def create_app(
    settings: Settings | None = None,
    *,
    token_verifier: IdentityTokenVerifier | None = None,
    notification_publisher: NotificationPublisher | None = None,
    capture_event_publisher: CaptureEventPublisher | None = None,
) -> FastAPI:
    active_settings = settings or Settings()
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
            model_spend_limit_dkk_micros=(
                active_settings.model_spend_limit_dkk_micros
            ),
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
            model_spend_limit_dkk_micros=(
                active_settings.model_spend_limit_dkk_micros
            ),
        )
        object_store = InMemoryObjectStore()
    if active_settings.environment == "production":
        assert active_settings.notification_topic is not None
        assert active_settings.image_topic is not None
        active_notification_publisher = notification_publisher or PubSubNotificationPublisher(
            topic=active_settings.notification_topic
        )
        active_capture_event_publisher = capture_event_publisher or PubSubCaptureEventPublisher(
            topic=active_settings.image_topic
        )
    else:
        active_notification_publisher = notification_publisher or InMemoryNotificationPublisher()
        active_capture_event_publisher = capture_event_publisher or InMemoryCaptureEventPublisher()
    container = Container(
        repository=repository,
        object_store=object_store,
        capture_service=CaptureService(
            repository=repository,
            object_store=object_store,
            event_publisher=active_capture_event_publisher,
        ),
        account_service=AccountProvisioningService(
            repository=repository,
            publisher=active_notification_publisher,
        ),
        notification_publisher=active_notification_publisher,
        capture_event_publisher=active_capture_event_publisher,
        inbound_mail_address_service=InboundMailAddressService(
            repository=repository,
            domain=active_settings.inbound_mail_domain,
        ),
        feedback_learning_service=FeedbackLearningService(repository),
        household_teaching_service=HouseholdTeachingService(repository),
        pattern_hypothesis_service=PatternHypothesisService(repository),
    )
    app = FastAPI(title="Gemini FoodLog API", version="0.1.0")
    app.state.container = container
    allowed_headers = ["Content-Type", "Idempotency-Key"]
    if active_settings.auth_backend == "firebase":
        allowed_headers.append("Authorization")
    else:
        allowed_headers.extend(["X-FoodLog-Local-User", "X-FoodLog-Preview-Secret"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=allowed_headers,
    )

    async def firebase_request_identity(
        authorization: str | None = Header(default=None),
    ) -> VerifiedIdentity:
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

    async def local_request_identity(
        x_foodlog_local_user: str | None = Header(default=None),
        x_foodlog_preview_secret: str | None = Header(default=None),
    ) -> VerifiedIdentity:
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

    request_identity = (
        firebase_request_identity
        if active_settings.auth_backend == "firebase"
        else local_request_identity
    )

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

    async def request_device_identity(
        authorization: str | None = Header(default=None),
    ) -> VerifiedDeviceIdentity:
        parts = authorization.split() if authorization else []
        token = parts[1] if len(parts) == 2 else ""
        if (
            len(parts) != 2
            or parts[0].casefold() != "foodlogcamera"
            or not token.startswith(DEVICE_TOKEN_PREFIX)
            or len(token) > 256
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A valid camera credential is required",
                headers={"WWW-Authenticate": "FoodLogCamera"},
            )
        try:
            return await container.repository.authenticate_device(
                sha256(token.encode()).hexdigest()
            )
        except InvalidDeviceCredential as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A valid camera credential is required",
                headers={"WWW-Authenticate": "FoodLogCamera"},
            ) from error

    async def firebase_capture_identity(
        authorization: str | None = Header(default=None),
    ) -> VerifiedIdentity | VerifiedDeviceIdentity:
        scheme = authorization.split(maxsplit=1)[0].casefold() if authorization else ""
        if scheme == "foodlogcamera":
            return await request_device_identity(authorization)
        return await firebase_request_identity(authorization)

    async def local_capture_identity(
        authorization: str | None = Header(default=None),
        x_foodlog_local_user: str | None = Header(default=None),
        x_foodlog_preview_secret: str | None = Header(default=None),
    ) -> VerifiedIdentity | VerifiedDeviceIdentity:
        scheme = authorization.split(maxsplit=1)[0].casefold() if authorization else ""
        if scheme == "foodlogcamera":
            return await request_device_identity(authorization)
        return await local_request_identity(
            x_foodlog_local_user=x_foodlog_local_user,
            x_foodlog_preview_secret=x_foodlog_preview_secret,
        )

    request_capture_identity = (
        firebase_capture_identity
        if active_settings.auth_backend == "firebase"
        else local_capture_identity
    )

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

    @app.exception_handler(WaitlistEntryNotFound)
    async def waitlist_entry_missing_handler(*_: object) -> Response:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

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
    app.add_exception_handler(PurchaseNotFound, resource_missing_handler)
    app.add_exception_handler(QuestionNotFound, resource_missing_handler)
    app.add_exception_handler(UserContextNoteNotFound, resource_missing_handler)
    app.add_exception_handler(KnowledgePageNotFound, resource_missing_handler)

    @app.exception_handler(CrossAccountAccess)
    async def cross_account_handler(*_: object) -> Response:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"idempotency_key_reused_with_different_payload"}',
            media_type="application/json",
        )

    @app.exception_handler(MealRevisionConflict)
    async def meal_revision_conflict_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"meal_revision_changed"}',
            media_type="application/json",
        )

    @app.exception_handler(KnowledgeRevisionConflict)
    async def knowledge_revision_conflict_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"knowledge_revision_changed"}',
            media_type="application/json",
        )

    async def invalid_knowledge_update_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content='{"detail":"invalid_knowledge_update"}',
            media_type="application/json",
        )

    app.add_exception_handler(InvalidKnowledgeTransition, invalid_knowledge_update_handler)
    app.add_exception_handler(InvalidKnowledgeProvenance, invalid_knowledge_update_handler)

    @app.exception_handler(InvalidMealCorrectionTarget)
    async def invalid_meal_correction_target_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content='{"detail":"invalid_meal_correction_target"}',
            media_type="application/json",
        )

    @app.exception_handler(InvalidMealFeedbackTransition)
    async def invalid_meal_feedback_transition_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content='{"detail":"invalid_meal_feedback_transition"}',
            media_type="application/json",
        )

    @app.exception_handler(QuestionAlreadyAnswered)
    async def question_already_answered_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"question_already_answered"}',
            media_type="application/json",
        )

    @app.exception_handler(QuestionSuperseded)
    async def question_superseded_handler(*_: object) -> Response:
        return Response(
            status_code=status.HTTP_409_CONFLICT,
            content='{"detail":"question_superseded"}',
            media_type="application/json",
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": active_settings.environment}

    @app.post("/v1/accounts", response_model=Account)
    async def provision_account(user_id: str = Depends(request_user_id)) -> Account:
        return await container.account_service.provision_account(user_id)

    @app.post("/v1/inbound-mail-address", response_model=InboundMailAddress)
    async def get_or_create_inbound_mail_address(
        response: Response,
        user_id: str = Depends(request_user_id),
    ) -> InboundMailAddress:
        response.headers["Cache-Control"] = "no-store"
        try:
            return await container.inbound_mail_address_service.get_or_create(user_id)
        except InboundAddressGenerationFailed as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="inbound_address_generation_failed",
            ) from exc

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

    @app.get("/v1/consents", response_model=ConsentPreferences)
    async def get_consent_preferences(
        identity: Annotated[VerifiedIdentity, Depends(request_identity)],
    ) -> ConsentPreferences:
        return await container.repository.consent_preferences(
            firebase_uid=identity.uid,
        )

    @app.post(
        "/v1/consents/launch-mail/withdraw",
        response_model=LaunchMailConsent,
    )
    async def withdraw_launch_mail_consent(
        identity: Annotated[VerifiedIdentity, Depends(request_identity)],
    ) -> LaunchMailConsent:
        return await container.repository.record_launch_mail_consent(
            owner_user_id=identity.uid,
            email_normalized=verified_email(identity),
            granted=False,
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

    @app.post("/v1/waitlist/withdraw", response_model=WaitlistEntry)
    async def withdraw_waitlist(
        identity: Annotated[VerifiedIdentity, Depends(request_identity)],
    ) -> WaitlistEntry:
        return await container.repository.withdraw_waitlist(
            firebase_uid=identity.uid,
        )

    @app.post("/v1/browser-cameras", response_model=BrowserCamera)
    async def create_browser_camera(
        request: BrowserCameraCreate,
        user_id: str = Depends(request_user_id),
    ) -> BrowserCamera:
        return await container.repository.create_browser_camera(
            user_id,
            request.name,
            request.client_instance_id,
        )

    @app.get("/v1/cameras", response_model=list[Camera])
    async def list_cameras(
        user_id: str = Depends(request_user_id),
    ) -> list[Camera]:
        return await container.repository.list_cameras(user_id)

    @app.post("/v1/cameras/{camera_id}/revoke", response_model=Camera)
    async def revoke_camera(
        camera_id: str,
        user_id: str = Depends(request_user_id),
    ) -> Camera:
        return await container.repository.revoke_camera(
            owner_user_id=user_id,
            camera_id=camera_id,
        )

    @app.post("/v1/device-cameras", response_model=DeviceCameraCredentialIssue)
    async def issue_device_camera(
        request: DeviceCameraCreate,
        response: Response,
        user_id: str = Depends(request_user_id),
    ) -> DeviceCameraCredentialIssue:
        for _ in range(3):
            credential = f"{DEVICE_TOKEN_PREFIX}{token_urlsafe(32)}"
            try:
                camera = await container.repository.issue_device_camera(
                    owner_user_id=user_id,
                    name=request.name,
                    credential_hash=sha256(credential.encode()).hexdigest(),
                    token_version=DEVICE_TOKEN_VERSION,
                )
                response.headers["Cache-Control"] = "no-store"
                return DeviceCameraCredentialIssue(
                    camera=camera,
                    credential=credential,
                )
            except DeviceCredentialCollision:
                continue
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="credential_generation_failed",
        )

    @app.post("/v1/device-cameras/{camera_id}/revoke", response_model=DeviceCamera)
    async def revoke_device_camera(
        camera_id: str,
        user_id: str = Depends(request_user_id),
    ) -> DeviceCamera:
        return await container.repository.revoke_device_camera(
            owner_user_id=user_id,
            camera_id=camera_id,
        )

    @app.get("/v1/device/status", response_model=DeviceSession)
    async def device_status(
        identity: Annotated[
            VerifiedDeviceIdentity,
            Depends(request_device_identity),
        ],
    ) -> DeviceSession:
        return DeviceSession(camera_id=identity.camera_id)

    @app.post(
        "/v1/captures",
        response_model=CaptureAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def upload_capture(
        metadata: Annotated[str, Form(min_length=2, max_length=4_096)],
        image: UploadFile,
        idempotency_key: Annotated[str, Header(min_length=8, max_length=128)],
        principal: Annotated[
            VerifiedIdentity | VerifiedDeviceIdentity,
            Depends(request_capture_identity),
        ],
    ) -> CaptureAccepted:
        try:
            envelope = CaptureEnvelopeV1.model_validate_json(metadata)
        except (ValidationError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid_capture_metadata",
            ) from error
        if envelope.captured_at > utc_now() + MAX_CAPTURE_FUTURE_SKEW:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="captured_at_too_far_in_future",
            )
        content, content_type = await validated_image_content(image)
        dimensions = image_dimensions(content, content_type)
        if dimensions is None:
            raise HTTPException(status_code=415, detail="Image dimensions could not be read")
        if dimensions != (envelope.width, envelope.height):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="image_dimensions_mismatch",
            )

        if isinstance(principal, VerifiedDeviceIdentity):
            if envelope.camera_id != principal.camera_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="camera_identity_mismatch",
                )
            if envelope.client_kind == "browser":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="camera_client_kind_mismatch",
                )
            owner_user_id = principal.owner_user_id
            camera = await container.repository.device_camera_for_identity(
                account_id=principal.account_id,
                camera_id=principal.camera_id,
            )
        else:
            if envelope.client_kind != "browser":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="camera_client_kind_mismatch",
                )
            owner_user_id = principal.uid
            camera = await container.repository.camera_for_owner(
                principal.uid,
                envelope.camera_id,
            )
            if camera.kind != "browser":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="camera_client_kind_mismatch",
                )

        return await container.capture_service.accept_capture(
            owner_user_id=owner_user_id,
            camera=camera,
            idempotency_key=idempotency_key,
            content_type=content_type,
            image=content,
            metadata=envelope,
        )

    @app.get("/v1/journal", response_model=list[MealEntry])
    async def list_journal(
        response: Response,
        user_id: str = Depends(request_user_id),
    ) -> list[MealEntry]:
        response.headers["Cache-Control"] = "private, no-store"
        return await container.repository.list_meals(user_id)

    @app.get("/v1/activities", response_model=list[MealEntry])
    async def list_activity_history(
        response: Response,
        activity_status: Annotated[MealStatus | None, Query(alias="status")] = None,
        user_id: str = Depends(request_user_id),
    ) -> list[MealEntry]:
        response.headers["Cache-Control"] = "private, no-store"
        return await container.repository.list_activity_history(
            user_id,
            status=activity_status,
        )

    @app.get("/v1/audit-events", response_model=list[AuditEvent])
    async def list_audit_events(
        response: Response,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        user_id: str = Depends(request_user_id),
    ) -> list[AuditEvent]:
        response.headers["Cache-Control"] = "private, no-store"
        return await container.repository.list_audit_events_for_owner(
            user_id,
            limit=limit,
        )

    @app.get("/v1/processing", response_model=list[CaptureProcessingView])
    async def list_processing(
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        user_id: str = Depends(request_user_id),
    ) -> list[CaptureProcessingView]:
        response.headers["Cache-Control"] = "private, no-store"
        captures = await container.repository.recent_captures_for_owner(
            user_id,
            limit=limit,
        )
        views: list[CaptureProcessingView] = []
        for capture in captures:
            grouping_job = await container.repository.job_for_account(
                capture.account_id,
                capture_grouping_job_id(capture.id),
            )
            inference_job = (
                await container.repository.job_for_account(
                    capture.account_id,
                    event_inference_job_id(capture.event_id),
                )
                if capture.event_id
                else None
            )
            views.append(
                capture_processing_view(
                    capture,
                    grouping_job=grouping_job,
                    inference_job=inference_job,
                )
            )
        return views

    @app.get("/v1/purchases", response_model=list[PurchaseSummaryView])
    async def list_purchases(
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        user_id: str = Depends(request_user_id),
    ) -> list[PurchaseSummaryView]:
        response.headers["Cache-Control"] = "private, no-store"
        purchases = await container.repository.list_purchases(user_id, limit=limit)
        return [purchase_summary_view(purchase) for purchase in purchases]

    @app.get("/v1/purchases/{purchase_id}", response_model=PurchaseDetailView)
    async def get_purchase(
        purchase_id: str,
        response: Response,
        user_id: str = Depends(request_user_id),
    ) -> PurchaseDetailView:
        response.headers["Cache-Control"] = "private, no-store"
        evidence = await container.repository.purchase_evidence_for_owner(
            user_id,
            purchase_id,
        )
        return purchase_detail_view(evidence)

    @app.get("/v1/meals/{meal_id}/revisions", response_model=list[MealRevision])
    async def list_meal_revisions(
        meal_id: str,
        response: Response,
        user_id: str = Depends(request_user_id),
    ) -> list[MealRevision]:
        response.headers["Cache-Control"] = "private, no-store"
        return await container.repository.list_meal_revisions(user_id, meal_id)

    @app.post(
        "/v1/meals/{meal_id}/feedback",
        response_model=MealFeedbackLearningResult,
    )
    async def record_meal_feedback(
        meal_id: str,
        request: MealFeedbackRequest,
        idempotency_key: str = Header(min_length=8, max_length=128),
        user_id: str = Depends(request_user_id),
    ) -> MealFeedbackLearningResult:
        return await container.feedback_learning_service.record(
            owner_user_id=user_id,
            meal_id=meal_id,
            request=request,
            idempotency_key=idempotency_key,
        )

    @app.get("/v1/questions", response_model=list[ClarificationQuestion])
    async def list_questions(
        response: Response,
        question_status: QuestionStatus | None = QuestionStatus.OPEN,
        question_kind: Annotated[QuestionKind | None, Query(alias="kind")] = None,
        user_id: str = Depends(request_user_id),
    ) -> list[ClarificationQuestion]:
        response.headers["Cache-Control"] = "private, no-store"
        questions = await container.repository.list_questions(
            user_id,
            question_status=question_status,
        )
        if question_kind is None:
            return questions
        return [question for question in questions if question.kind == question_kind]

    @app.post(
        "/v1/context-notes",
        response_model=UserContextNote,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_user_context_note(
        request: UserContextNoteCreate,
        idempotency_key: str = Header(min_length=8, max_length=128),
        user_id: str = Depends(request_user_id),
    ) -> UserContextNote:
        return await container.repository.create_user_context_note(
            owner_user_id=user_id,
            request=request,
            idempotency_key=idempotency_key,
        )

    @app.get("/v1/context-notes", response_model=list[UserContextNote])
    async def list_user_context_notes(
        include_inactive: bool = False,
        user_id: str = Depends(request_user_id),
    ) -> list[UserContextNote]:
        return await container.repository.list_user_context_notes(
            user_id,
            include_inactive=include_inactive,
        )

    @app.post(
        "/v1/context-notes/{note_id}/retire",
        response_model=UserContextNote,
    )
    async def retire_user_context_note(
        note_id: str,
        user_id: str = Depends(request_user_id),
    ) -> UserContextNote:
        return await container.repository.retire_user_context_note(
            owner_user_id=user_id,
            note_id=note_id,
        )

    @app.post(
        "/v1/knowledge",
        response_model=StableKnowledgeTeachingResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def teach_household_knowledge(
        request: StableKnowledgeTeachingCreate,
        response: Response,
        idempotency_key: str = Header(min_length=8, max_length=128),
        user_id: str = Depends(request_user_id),
    ) -> StableKnowledgeTeachingResult:
        response.headers["Cache-Control"] = "private, no-store"
        return await container.household_teaching_service.teach(
            owner_user_id=user_id,
            request=request,
            idempotency_key=idempotency_key,
        )

    @app.get("/v1/knowledge", response_model=list[KnowledgePage])
    async def list_household_knowledge(
        response: Response,
        include_retired: bool = False,
        limit: int = Query(default=50, ge=1, le=100),
        user_id: str = Depends(request_user_id),
    ) -> list[KnowledgePage]:
        response.headers["Cache-Control"] = "private, no-store"
        return await container.repository.list_knowledge_pages_for_owner(
            user_id,
            include_retired=include_retired,
            limit=limit,
        )

    @app.get("/v1/knowledge/{page_id}", response_model=KnowledgePageHistory)
    async def get_household_knowledge(
        page_id: str,
        response: Response,
        user_id: str = Depends(request_user_id),
    ) -> KnowledgePageHistory:
        response.headers["Cache-Control"] = "private, no-store"
        return await container.household_teaching_service.page_history(
            owner_user_id=user_id,
            page_id=page_id,
        )

    @app.post(
        "/v1/knowledge/{page_id}/correct",
        response_model=StableKnowledgeTeachingResult,
    )
    async def correct_household_knowledge(
        page_id: str,
        request: StableKnowledgeCorrectionCreate,
        response: Response,
        idempotency_key: str = Header(min_length=8, max_length=128),
        user_id: str = Depends(request_user_id),
    ) -> StableKnowledgeTeachingResult:
        response.headers["Cache-Control"] = "private, no-store"
        return await container.household_teaching_service.correct(
            owner_user_id=user_id,
            page_id=page_id,
            request=request,
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/v1/knowledge/{page_id}/retire",
        response_model=KnowledgeRevisionResult,
    )
    async def retire_household_knowledge(
        page_id: str,
        request: StableKnowledgeRetirementCreate,
        response: Response,
        idempotency_key: str = Header(min_length=8, max_length=128),
        user_id: str = Depends(request_user_id),
    ) -> KnowledgeRevisionResult:
        response.headers["Cache-Control"] = "private, no-store"
        return await container.household_teaching_service.retire(
            owner_user_id=user_id,
            page_id=page_id,
            request=request,
            idempotency_key=idempotency_key,
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

    @app.post(
        "/v1/questions/{question_id}/responses",
        response_model=QuestionResponseResult,
    )
    async def respond_to_question(
        question_id: str,
        request: QuestionResponseRequest,
        idempotency_key: str = Header(min_length=8, max_length=128),
        user_id: str = Depends(request_user_id),
    ) -> QuestionResponseResult:
        return await container.pattern_hypothesis_service.respond(
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
        content = await container.object_store.get(capture.account_id, capture.object_key)
        await record_audit_event(
            container.repository,
            account_id=capture.account_id,
            action=AuditAction.CAPTURE_IMAGE_READ,
            actor_kind=AuditActorKind.USER,
            source=AuditSource.API,
            subject_kind="capture",
            subject_id=capture.id,
        )
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
