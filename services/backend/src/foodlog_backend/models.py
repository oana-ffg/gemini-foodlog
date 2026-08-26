from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from unicodedata import normalize as unicode_normalize

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


class JobKind(StrEnum):
    CAPTURE_GROUPING = "capture_grouping"
    EVENT_INFERENCE = "event_inference"


class JobStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"


class ActivityEventStatus(StrEnum):
    OPEN = "open"
    INFERRED = "inferred"


class EntitlementMode(StrEnum):
    TRIAL = "trial"
    UNLIMITED = "unlimited"


class NotificationOutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DELIVERING = "delivering"
    DELIVERED = "delivered"


class CameraStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class DeviceCredentialStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class InboundMailAddressStatus(StrEnum):
    ACTIVE = "active"


class PurchaseDocumentKind(StrEnum):
    UNKNOWN = "unknown"
    ORDER_CONFIRMATION = "order_confirmation"
    FINAL_RECEIPT = "final_receipt"


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


class InboundMailAddress(BaseModel):
    id: Literal["current"] = "current"
    account_id: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=20, max_length=254)
    status: InboundMailAddressStatus = InboundMailAddressStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)


class InboundMailRoute(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=128)
    address_id: Literal["current"] = "current"
    status: InboundMailAddressStatus = InboundMailAddressStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)


def normalize_purchase_reference(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("purchase references cannot contain control characters")
    normalized = " ".join(unicode_normalize("NFKC", value).split()).casefold()
    if not normalized or len(normalized) > 128:
        raise ValueError("purchase references must contain 1-128 normalized characters")
    return normalized


class PurchaseDocumentCandidate(BaseModel):
    account_id: str = Field(min_length=1, max_length=128)
    raw_mail_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    merchant: Literal["nemlig"] = "nemlig"
    kind: PurchaseDocumentKind = PurchaseDocumentKind.UNKNOWN
    order_reference: str | None = Field(default=None, min_length=1, max_length=128)
    invoice_reference: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("order_reference", "invoice_reference", mode="before")
    @classmethod
    def normalize_reference(cls, value: str | None) -> str | None:
        return normalize_purchase_reference(value) if value is not None else None


class Purchase(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    merchant: Literal["nemlig"] = "nemlig"
    revision_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PurchaseDocument(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=128)
    purchase_id: str = Field(min_length=1, max_length=128)
    raw_mail_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    merchant: Literal["nemlig"] = "nemlig"
    kind: PurchaseDocumentKind = PurchaseDocumentKind.UNKNOWN
    revision_number: int = Field(ge=1)
    order_reference: str | None = Field(default=None, min_length=1, max_length=128)
    invoice_reference: str | None = Field(default=None, min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=utc_now)


class PurchaseIdentityAlias(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=128)
    purchase_id: str = Field(min_length=1, max_length=128)
    merchant: Literal["nemlig"] = "nemlig"
    kind: Literal["order", "invoice"]
    reference_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)


class PurchaseIdentityResult(BaseModel):
    purchase: Purchase
    document: PurchaseDocument
    duplicate: bool


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
    client_instance_id: str = Field(min_length=16, max_length=128)


class BrowserCamera(BaseModel):
    id: str
    account_id: str
    name: str
    client_instance_id_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    kind: Literal["browser"] = "browser"
    status: CameraStatus = CameraStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None


class DeviceCameraCreate(BaseModel):
    name: CameraName


class DeviceCamera(BaseModel):
    id: str
    account_id: str
    name: str
    kind: Literal["device"] = "device"
    status: CameraStatus = CameraStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None


Camera = Annotated[BrowserCamera | DeviceCamera, Field(discriminator="kind")]


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
    segment_id: str | None = Field(default=None, min_length=1, max_length=160)
    event_id: str | None = Field(default=None, min_length=1, max_length=160)
    status: CaptureStatus = CaptureStatus.ACCEPTED
    created_at: datetime = Field(default_factory=utc_now)


def capture_grouping_job_id(capture_id: str) -> str:
    return f"capture-grouping-{capture_id}"


def event_inference_job_id(event_id: str) -> str:
    return f"event-inference-{event_id}"


class DurableJob(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    account_id: str = Field(min_length=1, max_length=128)
    kind: JobKind
    subject_id: str = Field(min_length=1, max_length=160)
    subject_revision: int = Field(ge=1)
    status: JobStatus = JobStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=utc_now)
    lease_id: str | None = Field(default=None, min_length=1, max_length=128)
    lease_owner: str | None = Field(default=None, min_length=1, max_length=128)
    lease_expires_at: datetime | None = None
    last_error_code: str | None = Field(default=None, min_length=1, max_length=120)
    last_error_message: str | None = Field(default=None, min_length=1, max_length=2_000)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    @field_validator("available_at", "lease_expires_at", "created_at", "completed_at")
    @classmethod
    def job_timestamps_have_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("job timestamps must include a UTC offset")
        return value

    @model_validator(mode="after")
    def lease_and_completion_fields_are_consistent(self) -> "DurableJob":
        lease_fields = (self.lease_id, self.lease_owner, self.lease_expires_at)
        if self.status == JobStatus.LEASED:
            if any(value is None for value in lease_fields):
                raise ValueError("Leased jobs require a complete lease")
        elif any(value is not None for value in lease_fields):
            raise ValueError("Only leased jobs may retain lease fields")
        if (self.status == JobStatus.COMPLETED) != (self.completed_at is not None):
            raise ValueError("Completed jobs require completed_at and other jobs forbid it")
        return self


class ActivitySegment(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    account_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=160)
    camera_id: str = Field(min_length=1, max_length=128)
    source_key: str = Field(min_length=1, max_length=256)
    first_capture_at: datetime
    last_capture_at: datetime
    capture_count: int = Field(ge=1)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def segment_times_are_consistent(self) -> "ActivitySegment":
        for value in (self.first_capture_at, self.last_capture_at, self.created_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("segment timestamps must include a UTC offset")
        if self.last_capture_at < self.first_capture_at:
            raise ValueError("segment last_capture_at cannot precede first_capture_at")
        return self


class ActivityEvent(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    account_id: str = Field(min_length=1, max_length=128)
    status: ActivityEventStatus = ActivityEventStatus.OPEN
    current_revision: int = Field(default=1, ge=1)
    camera_ids: list[str] = Field(min_length=1, max_length=8)
    first_capture_at: datetime
    last_capture_at: datetime
    capture_count: int = Field(ge=1)
    grouping_policy_version: str = Field(min_length=1, max_length=80)
    meal_id: str | None = Field(default=None, min_length=1, max_length=160)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def event_times_are_consistent(self) -> "ActivityEvent":
        for value in (
            self.first_capture_at,
            self.last_capture_at,
            self.created_at,
            self.updated_at,
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("event timestamps must include a UTC offset")
        if self.last_capture_at < self.first_capture_at:
            raise ValueError("event last_capture_at cannot precede first_capture_at")
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("event camera IDs must be unique")
        return self


class ModelSpendReservation(BaseModel):
    id: str = Field(
        min_length=8,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    account_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=160)
    reserved_dkk_micros: int = Field(ge=1)
    model: str = Field(default="unknown", min_length=1, max_length=120)
    region: str = Field(default="unknown", min_length=1, max_length=80)
    purpose: str = Field(default="unspecified", min_length=1, max_length=80)
    prompt_version: str | None = Field(default=None, min_length=1, max_length=120)
    max_prompt_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_provider_attempts: int = Field(default=1, ge=1, le=10)
    retry_attempt: int = Field(default=0, ge=0, le=20)
    evaluation: bool = False
    status: Literal["reserved"] = "reserved"
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def reservation_timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("model spend reservation timestamp must include a UTC offset")
        return value


class ModelUsageRecord(BaseModel):
    id: str = Field(
        min_length=8,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    reservation_id: str = Field(min_length=8, max_length=160)
    account_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=160)
    invocation_id: str | None = Field(default=None, min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=120)
    model_version: str | None = Field(default=None, min_length=1, max_length=160)
    region: str = Field(min_length=1, max_length=80)
    prompt_version: str | None = Field(default=None, min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=80)
    retry_attempt: int = Field(ge=0, le=20)
    evaluation: bool
    outcome: Literal["succeeded", "failed"]
    prompt_tokens: int = Field(ge=0)
    response_tokens: int = Field(ge=0)
    thinking_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    actual_usd_nanos: int = Field(ge=0)
    actual_dkk_micros: int = Field(ge=0)
    reserved_dkk_micros: int = Field(ge=1)
    error_code: str | None = Field(default=None, min_length=1, max_length=160)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def usage_outcome_is_consistent(self) -> "ModelUsageRecord":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("model usage timestamp must include a UTC offset")
        if self.outcome == "succeeded":
            if min(self.prompt_tokens, self.response_tokens, self.total_tokens) <= 0:
                raise ValueError("successful model usage requires positive token counts")
            if self.actual_usd_nanos <= 0 or self.actual_dkk_micros <= 0:
                raise ValueError("successful model usage requires positive actual cost")
            if self.error_code is not None:
                raise ValueError("successful model usage cannot include an error code")
        elif (
            self.prompt_tokens
            or self.response_tokens
            or self.thinking_tokens
            or self.total_tokens
            or self.actual_usd_nanos
            or self.actual_dkk_micros
            or self.error_code is None
        ):
            raise ValueError("failed model usage requires zero usage and an error code")
        if self.actual_dkk_micros > self.reserved_dkk_micros:
            raise ValueError("actual model cost exceeds its reservation")
        return self


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
