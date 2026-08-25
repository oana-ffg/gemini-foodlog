from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CameraName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Confidence(StrEnum):
    CONFIDENT = "confident"
    LIKELY = "likely"
    UNCERTAIN = "uncertain"


class MealStatus(StrEnum):
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    CONTRADICTED = "contradicted"


class MealFeedbackKind(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"


class MealRevisionSource(StrEnum):
    INFERENCE = "inference"
    USER_FEEDBACK = "user_feedback"


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"


class CaptureStatus(StrEnum):
    ACCEPTED = "accepted"
    STORED = "stored"
    PROCESSED = "processed"


class EntitlementMode(StrEnum):
    TRIAL = "trial"
    UNLIMITED = "unlimited"


class NotificationOutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DELIVERING = "delivering"
    DELIVERED = "delivered"


class DeviceCameraStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class DeviceCredentialStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class Account(BaseModel):
    id: str
    owner_user_id: str
    entitlement_mode: EntitlementMode = EntitlementMode.TRIAL
    trial_image_limit: int | None = Field(default=None, ge=1)
    accepted_image_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def entitlement_fields_are_consistent(self) -> "Account":
        if self.entitlement_mode == EntitlementMode.TRIAL and self.trial_image_limit is None:
            raise ValueError("Trial accounts require an image limit")
        if (
            self.entitlement_mode == EntitlementMode.UNLIMITED
            and self.trial_image_limit is not None
        ):
            raise ValueError("Unlimited accounts cannot have a trial image limit")
        return self


class AccountCreatedOutbox(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    account_id: str
    kind: Literal["account_created"] = "account_created"
    entitlement_mode: EntitlementMode
    trial_image_limit: int | None = Field(default=None, ge=1)
    public_slot_number: int | None = Field(default=None, ge=1)
    status: NotificationOutboxStatus = NotificationOutboxStatus.PENDING
    publish_attempt_count: int = Field(default=0, ge=0)
    delivery_attempt_count: int = Field(default=0, ge=0)
    lease_id: str | None = Field(default=None, max_length=128)
    lease_expires_at: datetime | None = None
    provider_message_id: str | None = Field(default=None, max_length=256)
    provider_delivery_id: str | None = Field(default=None, max_length=256)
    last_error_code: str | None = Field(default=None, max_length=120)
    published_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def entitlement_fields_are_consistent(self) -> "AccountCreatedOutbox":
        if self.entitlement_mode == EntitlementMode.TRIAL:
            if self.trial_image_limit is None or self.public_slot_number is None:
                raise ValueError("Trial account notifications require limit and slot")
        elif self.trial_image_limit is not None or self.public_slot_number is not None:
            raise ValueError("Unlimited account notifications cannot have trial fields")
        return self


class LaunchMailConsentRequest(BaseModel):
    granted: bool


class LaunchMailConsent(BaseModel):
    id: str
    account_id: str
    actor_user_id: str
    email_normalized: str
    granted: bool
    policy_version: str
    kind: Literal["launch_mail"] = "launch_mail"
    created_at: datetime = Field(default_factory=utc_now)


class WaitlistJoinRequest(BaseModel):
    join: Literal[True]


class WaitlistEntry(BaseModel):
    id: str
    firebase_uid: str
    email_normalized: str
    policy_version: str
    reason: Literal["capacity"] = "capacity"
    mailing_list_opt_in: Literal[True] = True
    status: Literal["active"] = "active"
    created_at: datetime = Field(default_factory=utc_now)


class BrowserCameraCreate(BaseModel):
    name: CameraName


class BrowserCamera(BaseModel):
    id: str
    account_id: str
    name: str
    kind: str = "browser"
    created_at: datetime = Field(default_factory=utc_now)


class DeviceCameraCreate(BaseModel):
    name: CameraName


class DeviceCamera(BaseModel):
    id: str
    account_id: str
    name: str
    kind: Literal["device"] = "device"
    status: DeviceCameraStatus = DeviceCameraStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None


class DeviceCredentialRecord(BaseModel):
    credential_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: str
    camera_id: str
    token_version: int = Field(ge=1)
    status: DeviceCredentialStatus = DeviceCredentialStatus.ACTIVE
    issued_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class DeviceCameraCredentialIssue(BaseModel):
    camera: DeviceCamera
    credential: str


class VerifiedDeviceIdentity(BaseModel):
    owner_user_id: str
    account_id: str
    camera_id: str


class DeviceSession(BaseModel):
    camera_id: str
    status: Literal["active"] = "active"


class MotionMetadataV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detected: bool
    algorithm: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
    ]
    score: float | None = Field(default=None, ge=0, le=1)
    changed_pixel_ratio: float | None = Field(default=None, ge=0, le=1)
    threshold: float | None = Field(default=None, ge=0, le=1)


class CaptureEnvelopeV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    camera_id: str = Field(min_length=1, max_length=128)
    captured_at: datetime
    client_kind: Literal["browser", "simulator", "physical"]
    client_version: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
    ]
    sequence_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    sequence_number: int = Field(ge=0, le=2_147_483_647)
    burst_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    burst_frame_index: int | None = Field(default=None, ge=0, le=2_147_483_647)
    width: int = Field(ge=1, le=4_096)
    height: int = Field(ge=1, le=4_096)
    motion: MotionMetadataV1 | None = None

    @field_validator("captured_at")
    @classmethod
    def captured_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def burst_fields_are_consistent(self) -> "CaptureEnvelopeV1":
        if (self.burst_id is None) != (self.burst_frame_index is None):
            raise ValueError("burst_id and burst_frame_index must be provided together")
        return self


class CaptureRecord(BaseModel):
    id: str
    account_id: str
    camera_id: str
    idempotency_key: str
    content_type: str
    content_sha256: str
    object_key: str
    metadata: CaptureEnvelopeV1 | None = None
    status: CaptureStatus = CaptureStatus.ACCEPTED
    created_at: datetime = Field(default_factory=utc_now)


class MealComponent(BaseModel):
    name: str
    ingredients: list[str]
    preparation_methods: list[str]


class MealInference(BaseModel):
    title: str
    confidence: Confidence
    components: list[MealComponent]
    observations: list[str]
    alternatives: list[str]
    rationale: str
    clarification_question: str | None = None
    clarification_reason: str | None = None


class MealEntry(MealInference):
    id: str
    account_id: str
    capture_id: str
    status: MealStatus = MealStatus.PROVISIONAL
    revision_number: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)


class MealFeedbackRequest(BaseModel):
    kind: MealFeedbackKind
    actual_meal: str | None = Field(default=None, min_length=1, max_length=200)
    explanation: str | None = Field(default=None, min_length=1, max_length=2_000)

    @field_validator("actual_meal", "explanation")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must contain non-whitespace text")
        return stripped

    @model_validator(mode="after")
    def confirmation_has_no_correction_payload(self) -> "MealFeedbackRequest":
        if self.kind == MealFeedbackKind.CONFIRM and (
            self.actual_meal is not None or self.explanation is not None
        ):
            raise ValueError("confirmation cannot include correction fields")
        return self


class MealFeedback(BaseModel):
    id: str
    account_id: str
    meal_id: str
    kind: MealFeedbackKind
    actual_meal: str | None
    explanation: str | None
    idempotency_key: str
    question_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class MealRevision(BaseModel):
    id: str
    account_id: str
    meal_id: str
    number: int = Field(ge=1)
    status: MealStatus
    inference: MealInference
    source: MealRevisionSource
    feedback_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class MealFeedbackResult(BaseModel):
    feedback: MealFeedback
    revision: MealRevision


class ClarificationQuestion(BaseModel):
    id: str
    account_id: str
    meal_id: str
    prompt: str
    reason: str
    status: QuestionStatus = QuestionStatus.OPEN
    answer: str | None = None
    learning_tip: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    answered_at: datetime | None = None


class QuestionAnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=200)
    learning_tip: str | None = Field(default=None, min_length=1, max_length=2_000)

    @field_validator("answer")
    @classmethod
    def strip_answer(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must contain non-whitespace text")
        return stripped

    @field_validator("learning_tip")
    @classmethod
    def strip_learning_tip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must contain non-whitespace text")
        return stripped


class QuestionAnswerResult(BaseModel):
    question: ClarificationQuestion
    feedback: MealFeedback
    revision: MealRevision


class CaptureAccepted(BaseModel):
    capture_id: str
    accepted_image_count: int
    entitlement_mode: EntitlementMode
    trial_image_limit: int | None
    duplicate: bool
