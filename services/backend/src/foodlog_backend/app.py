import warnings
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from secrets import compare_digest, token_urlsafe
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image
from pydantic import ValidationError

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
    MealNotFound,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    QuestionSuperseded,
    TrialQuotaExhausted,
    WaitlistUnavailable,
)
from .firestore_repository import FirestoreRepository
from .image_events import (
    CaptureEventPublisher,
    InMemoryCaptureEventPublisher,
    PubSubCaptureEventPublisher,
)
from .inbound_mail import InboundMailAddressService
from .models import (
    Account,
    BrowserCamera,
    BrowserCameraCreate,
    Camera,
    CaptureAccepted,
    CaptureEnvelopeV1,
    ClarificationQuestion,
    DeviceCamera,
    DeviceCameraCreate,
    DeviceCameraCredentialIssue,
    DeviceSession,
    InboundMailAddress,
    LaunchMailConsent,
    LaunchMailConsentRequest,
    MealEntry,
    MealFeedbackRequest,
    MealFeedbackResult,
    MealRevision,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionResponseRequest,
    QuestionResponseResult,
    QuestionStatus,
    VerifiedDeviceIdentity,
    WaitlistEntry,
    WaitlistJoinRequest,
    utc_now,
)
from .notifications import (
    AccountProvisioningService,
    InMemoryNotificationPublisher,
    NotificationPublisher,
    PubSubNotificationPublisher,
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
            content='{"detail":"idempotency_key_reused_with_different_payload"}',
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
        return await container.repository.respond_to_question(
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
