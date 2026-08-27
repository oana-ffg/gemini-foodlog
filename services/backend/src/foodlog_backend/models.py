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

from .inference_schema import ActivityMealInferenceV1

CameraName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]
CorrectionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
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
    NOT_COOKING = "not_cooking"


class MealFeedbackKind(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    NOT_COOKING = "not_cooking"


class MealFeedbackLearningDisposition(StrEnum):
    REUSABLE = "reusable"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class MealRevisionSource(StrEnum):
    INFERENCE = "inference"
    USER_FEEDBACK = "user_feedback"


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    SUPERSEDED = "superseded"


class QuestionKind(StrEnum):
    EVENT_CLARIFICATION = "event_clarification"
    PATTERN_HYPOTHESIS = "pattern_hypothesis"


class QuestionResponseKind(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    REJECT = "reject"


class QuestionEvidenceKind(StrEnum):
    CAPTURE = "capture"
    MEAL_REVISION = "meal_revision"
    PURCHASE_DOCUMENT = "purchase_document"
    KNOWLEDGE_REVISION = "knowledge_revision"
    QUESTION = "question"
    INFERENCE_EVIDENCE = "inference_evidence"


class KnowledgeLifecycle(StrEnum):
    INFERRED = "inferred"
    REINFORCED = "reinforced"
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    RETIRED = "retired"


class KnowledgeBeliefStrength(StrEnum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class KnowledgeRevisionSource(StrEnum):
    AGENT_INFERENCE = "agent_inference"
    USER_FEEDBACK = "user_feedback"
    USER_STATEMENT = "user_statement"
    QUESTION_RESPONSE = "question_response"


class KnowledgeEvidenceKind(StrEnum):
    CAPTURE = "capture"
    MEAL_REVISION = "meal_revision"
    FEEDBACK = "feedback"
    QUESTION_RESPONSE = "question_response"
    PURCHASE_DOCUMENT = "purchase_document"
    USER_CONTEXT_NOTE = "user_context_note"
    KNOWLEDGE_REVISION = "knowledge_revision"


class KnowledgeEvidenceRole(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"


class UserContextNoteStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class UserContextNoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=4_000)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validity_window_is_ordered(self) -> "UserContextNoteCreate":
        for value in (self.valid_from, self.valid_until):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("context note validity timestamps must include a UTC offset")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("context note valid_until must be after valid_from")
        return self


class UserContextNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=128)
    author_user_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4_000)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    status: UserContextNoteStatus = UserContextNoteStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    retired_at: datetime | None = None

    @model_validator(mode="after")
    def lifecycle_and_window_are_consistent(self) -> "UserContextNote":
        UserContextNoteCreate(
            text=self.text,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
        )
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("context note creation timestamp must include a UTC offset")
        if self.status == UserContextNoteStatus.ACTIVE and self.retired_at is not None:
            raise ValueError("active context note cannot have retired_at")
        if self.status == UserContextNoteStatus.RETIRED and self.retired_at is None:
            raise ValueError("retired context note requires retired_at")
        if self.retired_at is not None and (
            self.retired_at.tzinfo is None or self.retired_at.utcoffset() is None
        ):
            raise ValueError("context note retirement timestamp must include a UTC offset")
        return self

    def is_active_at(self, moment: datetime) -> bool:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("context note evaluation timestamp must include a UTC offset")
        return (
            self.status == UserContextNoteStatus.ACTIVE
            and (self.valid_from is None or self.valid_from <= moment)
            and (self.valid_until is None or moment < self.valid_until)
        )


def _normalize_knowledge_claim_term(
    value: str,
    *,
    label: str,
    max_length: int = 200,
) -> str:
    normalized = " ".join(unicode_normalize("NFKC", value).casefold().split())
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{label} must contain 1-{max_length} normalized characters")
    return normalized


class KnowledgeClaim(BaseModel):
    """One normalized claim and the exact conditions in which it applies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=200)
    conditions: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("dimension")
    @classmethod
    def normalize_dimension(cls, value: str) -> str:
        return _normalize_knowledge_claim_term(
            value,
            label="claim dimension",
            max_length=120,
        )

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        return _normalize_knowledge_claim_term(value, label="claim value")

    @field_validator("conditions")
    @classmethod
    def normalize_conditions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    _normalize_knowledge_claim_term(value, label="claim condition")
                    for value in values
                }
            )
        )

    def applies_to(self, event_conditions: set[str]) -> bool:
        normalized_event = {
            _normalize_knowledge_claim_term(value, label="event condition")
            for value in event_conditions
        }
        return set(self.conditions).issubset(normalized_event)

    def is_no_broader_than(self, source: "KnowledgeClaim") -> bool:
        return (
            self.dimension == source.dimension
            and self.value == source.value
            and set(self.conditions).issuperset(source.conditions)
        )


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


class PurchaseItemDisposition(StrEnum):
    ORDERED = "ordered"
    DELIVERED = "delivered"


class PurchaseChargeKind(StrEnum):
    ITEMS_SUBTOTAL = "items_subtotal"
    DEPOSIT = "deposit"
    PACKING_FEE = "packing_fee"
    DELIVERY_FEE = "delivery_fee"
    CARD_FEE = "card_fee"
    TOTAL = "total"


class PurchaseReconciliationDisposition(StrEnum):
    DELIVERED_AS_ORDERED = "delivered_as_ordered"
    QUANTITY_CHANGED = "quantity_changed"
    REMOVED_OR_UNRESOLVED = "removed_or_unresolved"
    ADDED_OR_UNRESOLVED_SUBSTITUTION = "added_or_unresolved_substitution"


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
    latest_confirmation_document_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    latest_final_document_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
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


class PurchaseItemDraft(BaseModel):
    ordinal: int = Field(ge=1, le=250)
    name: str = Field(min_length=1, max_length=240)
    normalized_name: str = Field(min_length=1, max_length=240)
    disposition: PurchaseItemDisposition
    quantity: int = Field(ge=1, le=1_000)
    category: str | None = Field(default=None, min_length=1, max_length=120)
    unit_description: str | None = Field(default=None, min_length=1, max_length=160)
    unit_price_ore: int = Field(ge=0, le=100_000_000)
    included_discount_ore: int | None = Field(default=None, ge=0, le=100_000_000)
    line_total_ore: int = Field(ge=0, le=100_000_000)


class PurchaseChargeDraft(BaseModel):
    kind: PurchaseChargeKind
    amount_ore: int = Field(ge=0, le=100_000_000)
    description: str = Field(min_length=1, max_length=120)


class ParsedPurchaseDocument(BaseModel):
    parser_version: str = Field(min_length=1, max_length=80)
    kind: PurchaseDocumentKind
    items: list[PurchaseItemDraft] = Field(max_length=250)
    charges: list[PurchaseChargeDraft] = Field(max_length=20)
    included_vat_ore: int | None = Field(default=None, ge=0, le=100_000_000)

    @model_validator(mode="after")
    def parsed_document_is_internally_consistent(self) -> "ParsedPurchaseDocument":
        if not self.items:
            raise ValueError("purchase document must contain at least one item")
        if [item.ordinal for item in self.items] != list(range(1, len(self.items) + 1)):
            raise ValueError("purchase item ordinals must be contiguous")
        charge_kinds = [charge.kind for charge in self.charges]
        if len(charge_kinds) != len(set(charge_kinds)):
            raise ValueError("purchase charge kinds must be unique")
        return self


class PurchaseDocumentNormalization(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=128)
    purchase_id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_revision_number: int = Field(ge=1)
    document_kind: PurchaseDocumentKind
    parser_version: str = Field(min_length=1, max_length=80)
    normalization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_count: int = Field(ge=1, le=250)
    charge_count: int = Field(ge=0, le=20)
    included_vat_ore: int | None = Field(default=None, ge=0, le=100_000_000)
    created_at: datetime = Field(default_factory=utc_now)


class PurchaseItem(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=128)
    purchase_id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_revision_number: int = Field(ge=1)
    source_kind: PurchaseDocumentKind
    ordinal: int = Field(ge=1, le=250)
    name: str = Field(min_length=1, max_length=240)
    normalized_name: str = Field(min_length=1, max_length=240)
    disposition: PurchaseItemDisposition
    quantity: int = Field(ge=1, le=1_000)
    category: str | None = Field(default=None, min_length=1, max_length=120)
    unit_description: str | None = Field(default=None, min_length=1, max_length=160)
    unit_price_ore: int = Field(ge=0, le=100_000_000)
    included_discount_ore: int | None = Field(default=None, ge=0, le=100_000_000)
    line_total_ore: int = Field(ge=0, le=100_000_000)


class PurchaseCharge(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_id: str = Field(min_length=1, max_length=128)
    purchase_id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: PurchaseChargeKind
    amount_ore: int = Field(ge=0, le=100_000_000)
    description: str = Field(min_length=1, max_length=120)


class PurchaseReconciledItem(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_name: str = Field(min_length=1, max_length=240)
    display_name: str = Field(min_length=1, max_length=240)
    disposition: PurchaseReconciliationDisposition
    ordered_quantity: int | None = Field(default=None, ge=1, le=1_000)
    delivered_quantity: int | None = Field(default=None, ge=1, le=1_000)
    confirmation_item_ids: list[str] = Field(default_factory=list, max_length=20)
    final_item_ids: list[str] = Field(default_factory=list, max_length=20)


class PurchaseReconciliation(BaseModel):
    id: Literal["current"] = "current"
    account_id: str = Field(min_length=1, max_length=128)
    purchase_id: str = Field(min_length=1, max_length=128)
    confirmation_document_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    final_document_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reconciliation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_count: int = Field(ge=1, le=250)
    unresolved_item_count: int = Field(ge=0, le=250)
    has_unresolved_substitution_pairing: bool = False
    items: list[PurchaseReconciledItem] = Field(min_length=1, max_length=250)
    updated_at: datetime = Field(default_factory=utc_now)


class PurchaseNormalizationResult(BaseModel):
    normalization: PurchaseDocumentNormalization
    items: list[PurchaseItem] = Field(min_length=1, max_length=250)
    charges: list[PurchaseCharge] = Field(max_length=20)
    reconciliation: PurchaseReconciliation
    duplicate: bool


class PurchaseEvidenceBundle(BaseModel):
    """Complete account-internal evidence for one normalized purchase."""

    purchase: Purchase
    documents: list[PurchaseDocument]
    normalizations: list[PurchaseDocumentNormalization]
    items: list[PurchaseItem]
    charges: list[PurchaseCharge]
    reconciliation: PurchaseReconciliation | None = None

    @model_validator(mode="after")
    def evidence_is_tenant_and_purchase_consistent(self) -> "PurchaseEvidenceBundle":
        account_id = self.purchase.account_id
        purchase_id = self.purchase.id
        evidence = [
            *self.documents,
            *self.normalizations,
            *self.items,
            *self.charges,
        ]
        if self.reconciliation is not None:
            evidence.append(self.reconciliation)
        if any(
            item.account_id != account_id or item.purchase_id != purchase_id
            for item in evidence
        ):
            raise ValueError("purchase evidence crosses an account or purchase boundary")
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


class ConsentPreferences(BaseModel):
    launch_mail_opt_in: bool | None = None
    launch_mail_policy_version: str | None = None
    launch_mail_updated_at: datetime | None = None
    waitlist_status: Literal["not_joined", "active", "withdrawn"] = "not_joined"
    waitlist_policy_version: str | None = None
    waitlist_updated_at: datetime | None = None


class WaitlistJoinRequest(BaseModel):
    join: Literal[True]


class WaitlistEntry(BaseModel):
    id: str
    email_normalized: str | None
    policy_version: str
    reason: Literal["capacity"] = "capacity"
    mailing_list_opt_in: bool = True
    status: Literal["active", "withdrawn"] = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_withdrawn_at: datetime | None = None
    withdrawal_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def state_fields_are_consistent(self) -> "WaitlistEntry":
        if self.status == "active":
            if not self.email_normalized or not self.mailing_list_opt_in:
                raise ValueError("Active waitlist entries require an opted-in email")
        elif self.email_normalized is not None or self.mailing_list_opt_in:
            raise ValueError("Withdrawn waitlist entries cannot retain mailing details")
        if (self.last_withdrawn_at is None) != (self.withdrawal_count == 0):
            raise ValueError("Waitlist withdrawal audit fields are inconsistent")
        return self


class BrowserCameraCreate(BaseModel):
    name: CameraName
    client_instance_id: str = Field(min_length=16, max_length=128)


class CameraActivity(BaseModel):
    accepted_capture_count: int = Field(default=0, ge=0)
    last_capture_at: datetime | None = None


class BrowserCamera(CameraActivity):
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


class DeviceCamera(CameraActivity):
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
    captured_utc_offset_minutes: int | None = Field(default=None, ge=-840, le=840)
    segment_id: str | None = Field(default=None, min_length=1, max_length=160)
    event_id: str | None = Field(default=None, min_length=1, max_length=160)
    status: CaptureStatus = CaptureStatus.ACCEPTED
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def persisted_offset_matches_capture_timestamp(self) -> "CaptureRecord":
        if self.metadata is None or self.captured_utc_offset_minutes is None:
            return self
        offset = self.metadata.captured_at.utcoffset()
        assert offset is not None
        expected = int(offset.total_seconds() // 60)
        if self.captured_utc_offset_minutes != expected:
            raise ValueError("persisted capture offset does not match captured_at")
        return self


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
    max_billable_calls: int = Field(default=1, ge=1, le=10)
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
        elif self.error_code is None:
            raise ValueError("failed model usage requires an error code")
        elif bool(self.actual_usd_nanos) != bool(self.actual_dkk_micros):
            raise ValueError("failed model usage costs must both be zero or both be positive")
        elif self.total_tokens == 0 and (
            self.prompt_tokens
            or self.response_tokens
            or self.thinking_tokens
            or self.actual_usd_nanos
        ):
            raise ValueError("failed model usage cannot cost tokens when total tokens are zero")
        if self.actual_dkk_micros > self.reserved_dkk_micros:
            raise ValueError("actual model cost exceeds its reservation")
        return self


class AiTraceRecord(BaseModel):
    id: str = Field(pattern=r"^trace-[0-9a-f]{64}$")
    schema_version: Literal["application-visible-ai-trace-v1"] = (
        "application-visible-ai-trace-v1"
    )
    account_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=160)
    reservation_id: str = Field(min_length=8, max_length=160)
    root_trace_id: str = Field(pattern=r"^trace-[0-9a-f]{64}$")
    parent_trace_id: str | None = Field(default=None, pattern=r"^trace-[0-9a-f]{64}$")
    object_key: str = Field(min_length=1, max_length=512)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compressed_size: int = Field(ge=1, le=10_000_000)
    status: Literal["succeeded", "failed"]
    model: str = Field(min_length=1, max_length=120)
    model_version: str | None = Field(default=None, min_length=1, max_length=160)
    provider_invocation_id: str | None = Field(default=None, min_length=1, max_length=200)
    region: str = Field(min_length=1, max_length=80)
    prompt_version: str | None = Field(default=None, min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=80)
    retry_attempt: int = Field(ge=0, le=20)
    evaluation: bool
    prompt_tokens: int = Field(ge=0)
    response_tokens: int = Field(ge=0)
    thinking_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    actual_dkk_micros: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    error_code: str | None = Field(default=None, min_length=1, max_length=160)
    started_at: datetime
    completed_at: datetime
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def trace_metadata_is_consistent(self) -> "AiTraceRecord":
        for timestamp in (self.started_at, self.completed_at, self.created_at):
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("AI trace timestamps must include a UTC offset")
        if self.completed_at < self.started_at:
            raise ValueError("AI trace completion cannot precede its start")
        expected_key = f"accounts/{self.account_id}/traces/{self.id}.json.gz"
        if self.object_key != expected_key:
            raise ValueError("AI trace object key does not match its account and trace ID")
        if self.id == self.root_trace_id and self.parent_trace_id is not None:
            raise ValueError("root AI trace cannot have a parent")
        if self.id != self.root_trace_id and self.parent_trace_id != self.root_trace_id:
            raise ValueError("child AI trace must link directly to its root trace")
        if self.status == "succeeded" and self.error_code is not None:
            raise ValueError("successful AI trace cannot contain an error code")
        if self.status == "failed" and self.error_code is None:
            raise ValueError("failed AI trace requires an error code")
        return self


class MealComponent(BaseModel):
    name: str
    ingredients: list[str]
    preparation_methods: list[str]


class WholeMealCorrection(BaseModel):
    scope: Literal["meal"]
    title: CorrectionText
    components: list[MealComponent] | None = Field(default=None, max_length=20)


class ComponentCorrection(BaseModel):
    scope: Literal["component"]
    component_index: int = Field(ge=0)
    replacement: MealComponent


class IngredientCorrection(BaseModel):
    scope: Literal["ingredient"]
    component_index: int = Field(ge=0)
    ingredient_index: int = Field(ge=0)
    replacement: CorrectionText


class PreparationMethodCorrection(BaseModel):
    scope: Literal["preparation_method"]
    component_index: int = Field(ge=0)
    preparation_method_index: int = Field(ge=0)
    replacement: CorrectionText


MealCorrection = Annotated[
    WholeMealCorrection
    | ComponentCorrection
    | IngredientCorrection
    | PreparationMethodCorrection,
    Field(discriminator="scope"),
]


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
    event_id: str | None = Field(default=None, min_length=1, max_length=160)
    occurred_at: datetime | None = None
    occurred_utc_offset_minutes: int | None = Field(default=None, ge=-840, le=840)
    activity_hypothesis: ActivityMealInferenceV1 | None = None
    status: MealStatus = MealStatus.PROVISIONAL
    revision_number: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def structured_hypothesis_matches_materialized_meal(self) -> "MealEntry":
        if self.occurred_at is not None and (
            self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("meal occurrence timestamp must include a UTC offset")
        if self.occurred_at is None and self.occurred_utc_offset_minutes is not None:
            raise ValueError("meal occurrence offset requires an occurrence timestamp")
        if self.activity_hypothesis is None:
            return self
        if self.event_id != self.activity_hypothesis.event_id:
            raise ValueError("meal event must match its structured activity hypothesis")
        if self.capture_id not in self.activity_hypothesis.source_capture_ids:
            raise ValueError("meal capture must belong to its structured activity hypothesis")
        return self


class MealFeedbackRequest(BaseModel):
    kind: MealFeedbackKind
    actual_meal: str | None = Field(default=None, min_length=1, max_length=200)
    explanation: str | None = Field(default=None, min_length=1, max_length=2_000)
    correction: MealCorrection | None = None
    base_revision_number: int | None = Field(default=None, ge=1)
    learning_disposition: MealFeedbackLearningDisposition | None = None

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
    def correction_payload_is_consistent(self) -> "MealFeedbackRequest":
        if self.kind == MealFeedbackKind.NOT_COOKING:
            if (
                self.actual_meal is not None
                or self.correction is not None
                or self.base_revision_number is not None
                or self.learning_disposition is not None
            ):
                raise ValueError("not-cooking feedback cannot include correction fields")
            return self
        if self.kind == MealFeedbackKind.CONFIRM and (
            self.actual_meal is not None
            or self.explanation is not None
            or self.correction is not None
            or self.base_revision_number is not None
            or self.learning_disposition is not None
        ):
            raise ValueError("confirmation cannot include correction fields")
        if self.actual_meal is not None and self.correction is not None:
            raise ValueError("use either actual_meal or correction, not both")
        if self.correction is not None and self.base_revision_number is None:
            raise ValueError("targeted correction requires base_revision_number")
        if self.correction is None and self.base_revision_number is not None:
            raise ValueError("base_revision_number requires a targeted correction")
        if self.learning_disposition is not None and self.explanation is None:
            raise ValueError("learning disposition requires an explanation")
        if (
            self.learning_disposition == MealFeedbackLearningDisposition.REUSABLE
            and self.actual_meal is None
            and self.correction is None
        ):
            raise ValueError("reusable learning requires a corrected meal or target")
        return self


class MealFeedback(BaseModel):
    id: str
    account_id: str
    meal_id: str
    kind: MealFeedbackKind
    actual_meal: str | None
    explanation: str | None
    correction: MealCorrection | None = None
    base_revision_number: int | None = Field(default=None, ge=1)
    learning_disposition: MealFeedbackLearningDisposition | None = None
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
    activity_hypothesis: ActivityMealInferenceV1 | None = None
    source: MealRevisionSource
    feedback_id: str | None = None
    base_revision_number: int | None = Field(default=None, ge=1)
    correction: MealCorrection | None = None
    created_at: datetime = Field(default_factory=utc_now)


class MealFeedbackResult(BaseModel):
    feedback: MealFeedback
    revision: MealRevision


class KnowledgeEvidenceReference(BaseModel):
    kind: KnowledgeEvidenceKind
    id: str = Field(min_length=1, max_length=200)
    role: KnowledgeEvidenceRole
    note: str | None = Field(default=None, min_length=1, max_length=500)


class KnowledgeRevisionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=2_000)
    claim: KnowledgeClaim | None = None
    lifecycle: KnowledgeLifecycle
    belief_strength: KnowledgeBeliefStrength
    source: KnowledgeRevisionSource
    evidence: list[KnowledgeEvidenceReference] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("evidence")
    @classmethod
    def evidence_references_are_unique(
        cls,
        values: list[KnowledgeEvidenceReference],
    ) -> list[KnowledgeEvidenceReference]:
        identities = [(value.kind, value.id) for value in values]
        if len(set(identities)) != len(identities):
            raise ValueError("knowledge evidence references must be unique")
        return values

    @model_validator(mode="after")
    def contradiction_has_contradicting_evidence(self) -> "KnowledgeRevisionDraft":
        if self.lifecycle == KnowledgeLifecycle.CONTRADICTED and not any(
            item.role == KnowledgeEvidenceRole.CONTRADICTS for item in self.evidence
        ):
            raise ValueError("contradicted knowledge requires contradicting evidence")
        return self


class KnowledgeRevision(KnowledgeRevisionDraft):
    id: str
    account_id: str
    page_id: str
    number: int = Field(ge=1)
    base_revision_number: int | None = Field(default=None, ge=1)
    previous_revision_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class KnowledgePage(BaseModel):
    id: str
    account_id: str
    topic_key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=2_000)
    claim: KnowledgeClaim | None = None
    lifecycle: KnowledgeLifecycle
    belief_strength: KnowledgeBeliefStrength
    current_revision_number: int = Field(ge=1)
    current_revision_id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeRevisionResult(BaseModel):
    page: KnowledgePage
    revision: KnowledgeRevision


class StableKnowledgeTeachingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    statement: str = Field(min_length=1, max_length=2_000)


class StableKnowledgeCorrectionCreate(StableKnowledgeTeachingCreate):
    expected_revision_number: int = Field(ge=1)


class StableKnowledgeRetirementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision_number: int = Field(ge=1)
    reason: str | None = Field(default=None, min_length=1, max_length=2_000)


class StableKnowledgeTeachingResult(BaseModel):
    source_note: UserContextNote
    page: KnowledgePage
    revision: KnowledgeRevision


class KnowledgePageHistory(BaseModel):
    page: KnowledgePage
    revisions: list[KnowledgeRevision]


class QuestionEvidenceReference(BaseModel):
    kind: QuestionEvidenceKind
    id: str = Field(min_length=1, max_length=200)


class PatternEvidenceExample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    evidence: QuestionEvidenceReference
    occurred_at: datetime
    occurred_utc_offset_minutes: int | None = Field(default=None, ge=-840, le=840)
    summary: str = Field(min_length=1, max_length=500)

    @field_validator("occurred_at")
    @classmethod
    def occurrence_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("pattern evidence timestamps must include a UTC offset")
        return value


class ClarificationQuestion(BaseModel):
    id: str
    account_id: str
    kind: QuestionKind = QuestionKind.EVENT_CLARIFICATION
    meal_id: str | None = None
    event_id: str | None = None
    prompt: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=2_000)
    evidence: list[QuestionEvidenceReference] = Field(default_factory=list, max_length=20)
    choices: list[str] = Field(default_factory=list, max_length=8)
    tentative_claim: str | None = Field(default=None, min_length=1, max_length=2_000)
    pattern_claim: KnowledgeClaim | None = None
    pattern_observation_started_at: datetime | None = None
    pattern_observation_ended_at: datetime | None = None
    pattern_supporting_examples: list[PatternEvidenceExample] = Field(
        default_factory=list,
        max_length=20,
    )
    pattern_counterexamples: list[PatternEvidenceExample] = Field(
        default_factory=list,
        max_length=20,
    )
    pattern_prompt_version: str | None = Field(default=None, min_length=1, max_length=120)
    pattern_uncertainty: str | None = Field(default=None, min_length=1, max_length=1_000)
    pattern_evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pattern_topic_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    predecessor_question_id: str | None = Field(default=None, min_length=1, max_length=200)
    source_revision_number: int | None = Field(default=None, ge=1)
    status: QuestionStatus = QuestionStatus.OPEN
    answer: str | None = None
    learning_tip: str | None = None
    response_kind: QuestionResponseKind | None = None
    response_id: str | None = None
    superseded_by_question_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    answered_at: datetime | None = None
    superseded_at: datetime | None = None

    @field_validator("choices")
    @classmethod
    def strip_question_choices(cls, values: list[str]) -> list[str]:
        stripped = [value.strip() for value in values]
        if any(not value for value in stripped):
            raise ValueError("question choices must contain non-whitespace text")
        if len(set(value.casefold() for value in stripped)) != len(stripped):
            raise ValueError("question choices must be unique")
        return stripped

    @field_validator("evidence")
    @classmethod
    def question_evidence_is_unique(
        cls,
        values: list[QuestionEvidenceReference],
    ) -> list[QuestionEvidenceReference]:
        identities = [(value.kind, value.id) for value in values]
        if len(set(identities)) != len(identities):
            raise ValueError("question evidence references must be unique")
        return values

    @model_validator(mode="after")
    def validate_question_shape(self) -> "ClarificationQuestion":
        if self.kind == QuestionKind.EVENT_CLARIFICATION:
            if self.meal_id is None:
                raise ValueError("event questions require a meal")
            if self.tentative_claim is not None:
                raise ValueError("event questions cannot contain a pattern claim")
        elif self.meal_id is not None or self.event_id is not None:
            raise ValueError("pattern questions cannot target one meal or event")
        elif self.tentative_claim is None:
            raise ValueError("pattern questions require a tentative claim")
        elif not self.evidence:
            raise ValueError("pattern questions require supporting evidence")
        rich_pattern_fields = (
            self.pattern_claim,
            self.pattern_observation_started_at,
            self.pattern_observation_ended_at,
            self.pattern_prompt_version,
            self.pattern_evidence_hash,
            self.pattern_topic_key,
        )
        if any(value is not None for value in rich_pattern_fields):
            if self.kind != QuestionKind.PATTERN_HYPOTHESIS:
                raise ValueError("event questions cannot contain pattern hypothesis metadata")
            if any(value is None for value in rich_pattern_fields):
                raise ValueError("rich pattern hypotheses require complete metadata")
            assert self.pattern_observation_started_at is not None
            assert self.pattern_observation_ended_at is not None
            for value in (
                self.pattern_observation_started_at,
                self.pattern_observation_ended_at,
            ):
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("pattern observation timestamps must include a UTC offset")
            if self.pattern_observation_ended_at < self.pattern_observation_started_at:
                raise ValueError("pattern observation window must be ordered")
            if len(self.pattern_supporting_examples) < 2:
                raise ValueError("rich pattern hypotheses require at least two examples")
            example_identities = [
                (example.evidence.kind, example.evidence.id)
                for example in (
                    *self.pattern_supporting_examples,
                    *self.pattern_counterexamples,
                )
            ]
            if len(example_identities) != len(set(example_identities)):
                raise ValueError("pattern evidence examples must be unique")
            evidence_identities = {(item.kind, item.id) for item in self.evidence}
            if set(example_identities) != evidence_identities:
                raise ValueError("pattern examples must exactly match question evidence")
        if self.status == QuestionStatus.OPEN and (
            self.response_kind is not None
            or self.response_id is not None
            or self.answered_at is not None
            or self.superseded_by_question_id is not None
            or self.superseded_at is not None
        ):
            raise ValueError("open questions cannot contain a resolution")
        if (
            self.status == QuestionStatus.ANSWERED
            and (
                self.response_kind is None
                or self.response_id is None
                or self.answered_at is None
            )
            # Legacy persisted questions predate typed response fields.
            and (self.answer is None or self.answered_at is None)
        ):
            raise ValueError("answered questions require a response")
        if self.status == QuestionStatus.SUPERSEDED and self.superseded_at is None:
            raise ValueError("superseded questions require a timestamp")
        return self


class QuestionResponseRequest(BaseModel):
    kind: QuestionResponseKind
    correction: str | None = Field(default=None, min_length=1, max_length=500)
    explanation: str | None = Field(default=None, min_length=1, max_length=4_000)

    @field_validator("correction", "explanation")
    @classmethod
    def strip_optional_response_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must contain non-whitespace text")
        return stripped

    @model_validator(mode="after")
    def correction_matches_kind(self) -> "QuestionResponseRequest":
        if self.kind == QuestionResponseKind.CORRECT and self.correction is None:
            raise ValueError("a correction response requires corrected wording")
        if self.kind != QuestionResponseKind.CORRECT and self.correction is not None:
            raise ValueError("only a correction response may contain corrected wording")
        return self


class QuestionResponse(BaseModel):
    id: str
    account_id: str
    question_id: str
    kind: QuestionResponseKind
    correction: str | None
    explanation: str | None
    idempotency_key: str
    feedback_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class QuestionResponseResult(BaseModel):
    question: ClarificationQuestion
    response: QuestionResponse
    feedback: MealFeedback | None = None
    revision: MealRevision | None = None
    knowledge: KnowledgeRevisionResult | None = None


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
