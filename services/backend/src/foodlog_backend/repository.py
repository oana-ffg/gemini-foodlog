import asyncio
import json
from collections.abc import Iterable
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from unicodedata import normalize as unicode_normalize
from uuid import uuid4

from .errors import (
    AccountAlreadyProvisioned,
    AccountCapacityReached,
    AccountNotProvisioned,
    ActivityEventNotFound,
    CameraNotFound,
    CaptureNotFound,
    CrossAccountAccess,
    DeviceCredentialCollision,
    IdempotencyConflict,
    InboundAddressCollision,
    InboundAddressStateConflict,
    InvalidDeviceCredential,
    InvalidKnowledgeProvenance,
    InvalidKnowledgeTransition,
    InvalidMealCorrectionTarget,
    JobIdentityConflict,
    KnowledgePageNotFound,
    KnowledgeRevisionConflict,
    MealNotFound,
    MealRevisionConflict,
    ModelSpendLimitExceeded,
    ModelSpendReservationConflict,
    ModelUsageConflict,
    ModelUsageExceedsReservation,
    PurchaseDocumentConflict,
    PurchaseIdentityConflict,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    QuestionSuperseded,
    RawMailNotFound,
    TrialQuotaExhausted,
    WaitlistUnavailable,
)
from .grouping import (
    ACCOUNT_EVENT_HEAD_ID,
    CaptureGroupingResult,
    GroupingPolicy,
    capture_activity_time,
    capture_evidence_order,
    segment_identity,
)
from .inference_schema import ActivityMealInferenceV1, InferenceKind
from .models import (
    Account,
    AccountCreatedOutbox,
    ActivityEvent,
    ActivityEventStatus,
    ActivitySegment,
    BrowserCamera,
    Camera,
    CameraStatus,
    CaptureEnvelopeV1,
    CaptureRecord,
    CaptureStatus,
    ClarificationQuestion,
    ComponentCorrection,
    Confidence,
    DeviceCamera,
    DeviceCredentialRecord,
    DeviceCredentialStatus,
    DurableJob,
    EntitlementMode,
    InboundMailAddress,
    InboundMailRoute,
    IngredientCorrection,
    JobKind,
    JobStatus,
    KnowledgeEvidenceKind,
    KnowledgeLifecycle,
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeRevisionDraft,
    KnowledgeRevisionResult,
    LaunchMailConsent,
    MealComponent,
    MealEntry,
    MealFeedback,
    MealFeedbackKind,
    MealFeedbackRequest,
    MealFeedbackResult,
    MealInference,
    MealRevision,
    MealRevisionSource,
    MealStatus,
    ModelSpendReservation,
    ModelUsageRecord,
    NotificationOutboxStatus,
    PreparationMethodCorrection,
    Purchase,
    PurchaseDocument,
    PurchaseDocumentCandidate,
    PurchaseIdentityAlias,
    PurchaseIdentityResult,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionEvidenceKind,
    QuestionEvidenceReference,
    QuestionKind,
    QuestionResponse,
    QuestionResponseKind,
    QuestionResponseRequest,
    QuestionResponseResult,
    QuestionStatus,
    VerifiedDeviceIdentity,
    WaitlistEntry,
    WholeMealCorrection,
    capture_grouping_job_id,
    event_inference_job_id,
    utc_now,
)


def event_question_id(meal_id: str, revision_number: int) -> str:
    identity = f"event-question-v1:{meal_id}:{revision_number}"
    return sha256(identity.encode()).hexdigest()


def pattern_question_id(account_id: str, tentative_claim: str) -> str:
    normalized = " ".join(tentative_claim.casefold().split())
    return sha256(f"pattern-question-v1:{account_id}:{normalized}".encode()).hexdigest()


def normalize_knowledge_topic(value: str) -> str:
    normalized = " ".join(unicode_normalize("NFKC", value).casefold().split())
    if not normalized or len(normalized) > 160:
        raise ValueError("knowledge topic must contain 1-160 normalized characters")
    return normalized


def knowledge_page_id(account_id: str, topic_key: str) -> str:
    normalized = normalize_knowledge_topic(topic_key)
    return sha256(f"knowledge-page-v1:{account_id}:{normalized}".encode()).hexdigest()


def knowledge_revision_request_hash(
    *,
    topic_key: str,
    expected_revision_number: int | None,
    draft: KnowledgeRevisionDraft,
) -> str:
    draft_payload = draft.model_dump(mode="json")
    # `claim` was added after the first production wiki revisions. Omitting only
    # its absent value preserves the exact legacy request hash while still making
    # every structured claim part of new idempotency identity.
    if draft_payload.get("claim") is None:
        draft_payload.pop("claim", None)
    payload = {
        "topic_key": normalize_knowledge_topic(topic_key),
        "expected_revision_number": expected_revision_number,
        "draft": draft_payload,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_KNOWLEDGE_TRANSITIONS = {
    KnowledgeLifecycle.INFERRED: frozenset(
        {
            KnowledgeLifecycle.INFERRED,
            KnowledgeLifecycle.REINFORCED,
            KnowledgeLifecycle.CONFIRMED,
            KnowledgeLifecycle.CONTRADICTED,
            KnowledgeLifecycle.RETIRED,
        }
    ),
    KnowledgeLifecycle.REINFORCED: frozenset(
        {
            KnowledgeLifecycle.REINFORCED,
            KnowledgeLifecycle.CONFIRMED,
            KnowledgeLifecycle.CONTRADICTED,
            KnowledgeLifecycle.RETIRED,
        }
    ),
    KnowledgeLifecycle.CONFIRMED: frozenset(
        {
            KnowledgeLifecycle.CONFIRMED,
            KnowledgeLifecycle.CONTRADICTED,
            KnowledgeLifecycle.RETIRED,
        }
    ),
    KnowledgeLifecycle.CONTRADICTED: frozenset(
        {
            KnowledgeLifecycle.INFERRED,
            KnowledgeLifecycle.REINFORCED,
            KnowledgeLifecycle.CONFIRMED,
            KnowledgeLifecycle.CONTRADICTED,
            KnowledgeLifecycle.RETIRED,
        }
    ),
    KnowledgeLifecycle.RETIRED: frozenset(),
}


def validate_knowledge_revision(
    *,
    previous: KnowledgeRevision | None,
    expected_revision_number: int | None,
    draft: KnowledgeRevisionDraft,
) -> None:
    if previous is None:
        if expected_revision_number is not None:
            raise KnowledgeRevisionConflict
        return
    if expected_revision_number != previous.number:
        raise KnowledgeRevisionConflict
    if draft.lifecycle not in _KNOWLEDGE_TRANSITIONS[previous.lifecycle]:
        raise InvalidKnowledgeTransition
    if not any(
        evidence.kind == KnowledgeEvidenceKind.KNOWLEDGE_REVISION
        and evidence.id == previous.id
        for evidence in draft.evidence
    ):
        raise InvalidKnowledgeProvenance


def materialize_knowledge_page(
    *,
    topic_key: str,
    revision: KnowledgeRevision,
    created_at: datetime,
) -> KnowledgePage:
    return KnowledgePage(
        id=revision.page_id,
        account_id=revision.account_id,
        topic_key=normalize_knowledge_topic(topic_key),
        title=revision.title,
        statement=revision.statement,
        claim=revision.claim,
        lifecycle=revision.lifecycle,
        belief_strength=revision.belief_strength,
        current_revision_number=revision.number,
        current_revision_id=revision.id,
        created_at=created_at,
        updated_at=revision.created_at,
    )


def validate_focused_question_prompt(prompt: str) -> None:
    normalized = " ".join(prompt.casefold().split())
    forbidden = (
        "what meal",
        "which meal",
        "what ingredient",
        "what were you cooking",
        "what are you cooking",
    )
    if any(phrase in normalized for phrase in forbidden):
        raise ValueError("question must distinguish specific hypotheses, not request a label")


def event_question_from_hypothesis(
    *,
    meal: MealEntry,
    revision: MealRevision,
    hypothesis: ActivityMealInferenceV1,
    created_at: datetime,
) -> ClarificationQuestion | None:
    if hypothesis.question is None:
        return None
    return ClarificationQuestion(
        id=event_question_id(meal.id, revision.number),
        account_id=meal.account_id,
        kind=QuestionKind.EVENT_CLARIFICATION,
        meal_id=meal.id,
        event_id=meal.event_id,
        prompt=hypothesis.question.prompt,
        reason=hypothesis.question.justification,
        evidence=[
            QuestionEvidenceReference(
                kind=QuestionEvidenceKind.MEAL_REVISION,
                id=revision.id,
            ),
            *(
                QuestionEvidenceReference(
                    kind=QuestionEvidenceKind.INFERENCE_EVIDENCE,
                    id=evidence_id,
                )
                for evidence_id in hypothesis.question.evidence_ids
            ),
        ],
        choices=hypothesis.question.candidate_labels,
        source_revision_number=revision.number,
        created_at=created_at,
    )


def validate_enqueueable_job(job: DurableJob) -> None:
    if (
        job.status != JobStatus.PENDING
        or job.attempt_count != 0
        or job.last_error_code is not None
        or job.last_error_message is not None
    ):
        raise ValueError("New job revisions must start as clean pending work")


def validate_capture_scope(
    *,
    account: Account,
    camera: BrowserCamera | DeviceCamera,
    capture_id: str,
    content_type: str,
    object_key: str,
    metadata: CaptureEnvelopeV1 | None,
) -> None:
    if camera.account_id != account.id:
        raise CrossAccountAccess
    if metadata is not None and metadata.camera_id != camera.id:
        raise CameraNotFound
    extension = {"image/jpeg": "jpg", "image/png": "png"}.get(content_type)
    if extension is None:
        raise ValueError("Unsupported capture content type")
    if object_key != f"accounts/{account.id}/captures/{capture_id}.{extension}":
        raise CrossAccountAccess


def purchase_identity_alias_id(*, merchant: str, kind: str, reference: str) -> str:
    return sha256(f"{merchant}\0{kind}\0{reference}".encode()).hexdigest()


def purchase_identity_aliases(
    candidate: PurchaseDocumentCandidate,
) -> list[tuple[str, str, str]]:
    aliases = []
    if candidate.order_reference is not None:
        aliases.append(
            (
                purchase_identity_alias_id(
                    merchant=candidate.merchant,
                    kind="order",
                    reference=candidate.order_reference,
                ),
                "order",
                candidate.order_reference,
            )
        )
    if candidate.invoice_reference is not None:
        aliases.append(
            (
                purchase_identity_alias_id(
                    merchant=candidate.merchant,
                    kind="invoice",
                    reference=candidate.invoice_reference,
                ),
                "invoice",
                candidate.invoice_reference,
            )
        )
    return aliases


def validate_purchase_document_retry(
    existing: PurchaseDocument,
    candidate: PurchaseDocumentCandidate,
) -> None:
    if (
        existing.account_id != candidate.account_id
        or existing.raw_mail_id != candidate.raw_mail_id
        or existing.raw_content_sha256 != candidate.raw_content_sha256
        or existing.merchant != candidate.merchant
        or existing.kind != candidate.kind
        or existing.order_reference != candidate.order_reference
        or existing.invoice_reference != candidate.invoice_reference
    ):
        raise PurchaseDocumentConflict


def validate_purchase_identity_alias(
    alias: PurchaseIdentityAlias,
    *,
    candidate: PurchaseDocumentCandidate,
    alias_id: str,
    kind: str,
    reference: str,
) -> None:
    if (
        alias.id != alias_id
        or alias.account_id != candidate.account_id
        or alias.merchant != candidate.merchant
        or alias.kind != kind
        or alias.reference_hash != sha256(reference.encode()).hexdigest()
    ):
        raise PurchaseIdentityConflict


def materialize_activity_hypothesis(
    *,
    event: ActivityEvent,
    captures: list[CaptureRecord],
    hypothesis: ActivityMealInferenceV1,
    meal_id: str,
    revision_number: int,
    created_at: datetime,
) -> MealEntry:
    """Build the journal projection while retaining the complete validated hypothesis."""
    if hypothesis.event_id != event.id:
        raise ValueError("Activity hypothesis does not belong to the claimed event")
    ordered_capture_ids = [capture.id for capture in captures]
    if hypothesis.source_capture_ids != ordered_capture_ids:
        raise ValueError("Activity hypothesis does not cover the canonical event evidence")
    if hypothesis.kind == InferenceKind.UNKNOWN_ACTIVITY:
        title = "Unknown kitchen activity"
    else:
        assert hypothesis.best_guess is not None
        title = hypothesis.best_guess
    return MealEntry(
        id=meal_id,
        account_id=event.account_id,
        capture_id=captures[0].id,
        event_id=event.id,
        occurred_at=event.last_capture_at,
        activity_hypothesis=hypothesis,
        title=title,
        confidence=Confidence(hypothesis.confidence.value),
        components=[
            MealComponent(
                name=component.name,
                ingredients=component.ingredients,
                preparation_methods=component.preparation_methods,
            )
            for component in hypothesis.components
        ],
        observations=[item.description for item in hypothesis.direct_observations],
        alternatives=[item.label for item in hypothesis.alternatives],
        rationale=hypothesis.rationale,
        clarification_question=(hypothesis.question.prompt if hypothesis.question else None),
        clarification_reason=(
            hypothesis.question.justification if hypothesis.question else None
        ),
        status=MealStatus.PROVISIONAL,
        revision_number=revision_number,
        created_at=created_at,
    )


class Repository(Protocol):
    async def provision_account(self, owner_user_id: str) -> Account: ...

    async def claim_account_notification_for_publish(
        self,
        *,
        account_id: str,
        lease_id: str,
        lease_expires_at: datetime,
    ) -> AccountCreatedOutbox | None: ...

    async def mark_account_notification_published(
        self,
        *,
        event_id: str,
        lease_id: str,
        provider_message_id: str,
    ) -> bool: ...

    async def release_account_notification_publish(
        self,
        *,
        event_id: str,
        lease_id: str,
        error_code: str,
    ) -> bool: ...

    async def claim_account_notification_for_delivery(
        self,
        *,
        event_id: str,
        lease_id: str,
        lease_expires_at: datetime,
    ) -> AccountCreatedOutbox | None: ...

    async def mark_account_notification_delivered(
        self,
        *,
        event_id: str,
        lease_id: str,
        provider_delivery_id: str,
    ) -> bool: ...

    async def release_account_notification_delivery(
        self,
        *,
        event_id: str,
        lease_id: str,
        error_code: str,
    ) -> bool: ...

    async def account_for_owner(self, owner_user_id: str) -> Account: ...

    async def create_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        address: str,
        address_hash: str,
    ) -> InboundMailAddress: ...

    async def attach_purchase_document(
        self,
        candidate: PurchaseDocumentCandidate,
    ) -> PurchaseIdentityResult: ...

    async def record_launch_mail_consent(
        self,
        *,
        owner_user_id: str,
        email_normalized: str,
        granted: bool,
        policy_version: str,
    ) -> LaunchMailConsent: ...

    async def join_waitlist(
        self,
        *,
        firebase_uid: str,
        email_normalized: str,
        policy_version: str,
    ) -> WaitlistEntry: ...

    async def issue_device_camera(
        self,
        *,
        owner_user_id: str,
        name: str,
        credential_hash: str,
        token_version: int,
    ) -> DeviceCamera: ...

    async def authenticate_device(
        self,
        credential_hash: str,
    ) -> VerifiedDeviceIdentity: ...

    async def revoke_device_camera(
        self,
        *,
        owner_user_id: str,
        camera_id: str,
    ) -> DeviceCamera: ...

    async def list_cameras(self, owner_user_id: str) -> list[Camera]: ...

    async def revoke_camera(
        self,
        *,
        owner_user_id: str,
        camera_id: str,
    ) -> Camera: ...

    async def device_camera_for_identity(
        self,
        *,
        account_id: str,
        camera_id: str,
    ) -> DeviceCamera: ...

    async def create_browser_camera(
        self,
        owner_user_id: str,
        name: str,
        client_instance_id: str,
    ) -> BrowserCamera: ...

    async def camera_for_owner(self, owner_user_id: str, camera_id: str) -> Camera: ...

    async def reserve_capture(
        self,
        *,
        capture_id: str,
        account: Account,
        camera: BrowserCamera | DeviceCamera,
        idempotency_key: str,
        content_type: str,
        content_sha256: str,
        object_key: str,
        metadata: CaptureEnvelopeV1 | None = None,
    ) -> tuple[CaptureRecord, Account, bool]: ...

    async def cancel_capture(
        self,
        *,
        account_id: str,
        capture: CaptureRecord,
    ) -> None: ...

    async def mark_stored(self, *, account_id: str, capture_id: str) -> None: ...

    async def mark_processed(self, *, account_id: str, capture_id: str) -> None: ...

    async def enqueue_job(self, job: DurableJob) -> DurableJob: ...

    async def job_for_account(self, account_id: str, job_id: str) -> DurableJob | None: ...

    async def claim_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> DurableJob | None: ...

    async def complete_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
    ) -> bool: ...

    async def release_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        available_at: datetime,
        error_code: str,
        error_message: str,
    ) -> bool: ...

    async def group_capture(
        self,
        *,
        account_id: str,
        capture_id: str,
        lease_id: str,
        lease_owner: str,
        policy: GroupingPolicy,
    ) -> CaptureGroupingResult | None: ...

    async def event_evidence_for_account(
        self,
        *,
        account_id: str,
        event_id: str,
    ) -> tuple[ActivityEvent, list[CaptureRecord]]: ...

    async def publish_event_inference(
        self,
        *,
        account_id: str,
        event_id: str,
        expected_event_revision: int,
        lease_id: str,
        lease_owner: str,
        hypothesis: ActivityMealInferenceV1,
    ) -> MealEntry | None: ...

    async def reserve_model_spend(
        self,
        reservation: ModelSpendReservation,
    ) -> ModelSpendReservation: ...

    async def model_usage_for_reservation(
        self,
        *,
        account_id: str,
        reservation_id: str,
    ) -> ModelUsageRecord | None: ...

    async def record_model_usage(self, usage: ModelUsageRecord) -> ModelUsageRecord: ...

    async def save_meal(self, *, account_id: str, meal: MealEntry) -> MealEntry: ...

    async def open_question(
        self,
        *,
        account_id: str,
        meal: MealEntry,
        prompt: str,
        reason: str,
    ) -> ClarificationQuestion: ...

    async def open_pattern_question(
        self,
        *,
        account_id: str,
        prompt: str,
        reason: str,
        tentative_claim: str,
        evidence: list[QuestionEvidenceReference],
        supersedes_question_id: str | None = None,
    ) -> ClarificationQuestion: ...

    async def list_meals(self, owner_user_id: str) -> list[MealEntry]: ...

    async def meal_for_owner(self, owner_user_id: str, meal_id: str) -> MealEntry: ...

    async def list_meal_revisions(self, owner_user_id: str, meal_id: str) -> list[MealRevision]: ...

    async def record_knowledge_revision(
        self,
        *,
        account_id: str,
        topic_key: str,
        expected_revision_number: int | None,
        draft: KnowledgeRevisionDraft,
        idempotency_key: str,
    ) -> KnowledgeRevisionResult: ...

    async def knowledge_page_for_owner(
        self,
        owner_user_id: str,
        page_id: str,
    ) -> KnowledgePage: ...

    async def list_knowledge_revisions(
        self,
        owner_user_id: str,
        page_id: str,
    ) -> list[KnowledgeRevision]: ...

    async def record_meal_feedback(
        self,
        *,
        owner_user_id: str,
        meal_id: str,
        request: MealFeedbackRequest,
        idempotency_key: str,
    ) -> MealFeedbackResult: ...

    async def list_questions(
        self,
        owner_user_id: str,
        *,
        question_status: QuestionStatus | None = None,
    ) -> list[ClarificationQuestion]: ...

    async def answer_question(
        self,
        *,
        owner_user_id: str,
        question_id: str,
        request: QuestionAnswerRequest,
        idempotency_key: str,
    ) -> QuestionAnswerResult: ...

    async def respond_to_question(
        self,
        *,
        owner_user_id: str,
        question_id: str,
        request: QuestionResponseRequest,
        idempotency_key: str,
    ) -> QuestionResponseResult: ...

    async def capture_for_owner(self, owner_user_id: str, capture_id: str) -> CaptureRecord: ...


class InMemoryRepository:
    """Concurrency-safe local adapter mirroring the required Firestore invariants."""

    def __init__(
        self,
        *,
        public_account_limit: int,
        trial_image_limit: int,
        unlimited_owner_user_ids: set[str] | None = None,
        model_spend_limit_dkk_micros: int = 400_000_000,
    ) -> None:
        if model_spend_limit_dkk_micros < 1:
            raise ValueError("model spend limit must be positive")
        self._public_account_limit = public_account_limit
        self._trial_image_limit = trial_image_limit
        self._unlimited_owner_user_ids = frozenset(unlimited_owner_user_ids or set())
        self._model_spend_limit_dkk_micros = model_spend_limit_dkk_micros
        self._model_spend_reserved_dkk_micros = 0
        self._model_spend_actual_dkk_micros = 0
        self._model_spend_reservations: dict[str, ModelSpendReservation] = {}
        self._model_usage: dict[str, ModelUsageRecord] = {}
        self._accounts: dict[str, Account] = {}
        self._account_by_owner: dict[str, str] = {}
        self._notification_outbox: dict[str, AccountCreatedOutbox] = {}
        self._launch_consents: dict[str, LaunchMailConsent] = {}
        self._waitlist_by_email_hash: dict[str, WaitlistEntry] = {}
        self._inbound_mail_addresses: dict[str, InboundMailAddress] = {}
        self._inbound_mail_routes: dict[str, InboundMailRoute] = {}
        self._published_raw_mail: dict[tuple[str, str], str] = {}
        self._purchases: dict[tuple[str, str], Purchase] = {}
        self._purchase_documents: dict[tuple[str, str], PurchaseDocument] = {}
        self._purchase_aliases: dict[tuple[str, str], PurchaseIdentityAlias] = {}
        self._device_cameras: dict[str, DeviceCamera] = {}
        self._device_credentials: dict[str, DeviceCredentialRecord] = {}
        self._cameras: dict[str, BrowserCamera] = {}
        self._browser_camera_by_instance: dict[tuple[str, str], str] = {}
        self._captures: dict[str, CaptureRecord] = {}
        self._capture_by_idempotency: dict[tuple[str, str], str] = {}
        self._jobs: dict[tuple[str, str], DurableJob] = {}
        self._events: dict[tuple[str, str], ActivityEvent] = {}
        self._segments: dict[tuple[str, str], ActivitySegment] = {}
        self._event_head_by_source: dict[tuple[str, str], str] = {}
        self._meals: dict[str, MealEntry] = {}
        self._meal_by_capture: dict[str, str] = {}
        self._meal_revisions: dict[str, list[MealRevision]] = {}
        self._knowledge_pages: dict[str, KnowledgePage] = {}
        self._knowledge_revisions: dict[str, list[KnowledgeRevision]] = {}
        self._knowledge_revision_requests: dict[
            tuple[str, str], tuple[str, KnowledgeRevisionResult]
        ] = {}
        self._feedback: dict[str, MealFeedback] = {}
        self._feedback_by_idempotency: dict[tuple[str, str], str] = {}
        self._revision_by_feedback: dict[str, MealRevision] = {}
        self._questions: dict[str, ClarificationQuestion] = {}
        self._question_by_meal: dict[str, str] = {}
        self._question_responses: dict[str, QuestionResponse] = {}
        self._question_response_by_idempotency: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def provision_account(self, owner_user_id: str) -> Account:
        async with self._lock:
            existing_id = self._account_by_owner.get(owner_user_id)
            if existing_id:
                return self._accounts[existing_id].model_copy(deep=True)
            entitlement_mode = (
                EntitlementMode.UNLIMITED
                if owner_user_id in self._unlimited_owner_user_ids
                else EntitlementMode.TRIAL
            )
            public_account_count = sum(
                account.entitlement_mode == EntitlementMode.TRIAL
                for account in self._accounts.values()
            )
            if (
                entitlement_mode == EntitlementMode.TRIAL
                and public_account_count >= self._public_account_limit
            ):
                raise AccountCapacityReached
            account = Account(
                id=str(uuid4()),
                owner_user_id=owner_user_id,
                entitlement_mode=entitlement_mode,
                trial_image_limit=(
                    self._trial_image_limit if entitlement_mode == EntitlementMode.TRIAL else None
                ),
            )
            self._accounts[account.id] = account
            self._account_by_owner[owner_user_id] = account.id
            event = AccountCreatedOutbox(
                id=f"account-created-{account.id}",
                account_id=account.id,
                entitlement_mode=account.entitlement_mode,
                trial_image_limit=account.trial_image_limit,
                public_slot_number=(
                    public_account_count + 1
                    if account.entitlement_mode == EntitlementMode.TRIAL
                    else None
                ),
                created_at=account.created_at,
            )
            self._notification_outbox[event.id] = event
            return account.model_copy(deep=True)

    async def claim_account_notification_for_publish(
        self,
        *,
        account_id: str,
        lease_id: str,
        lease_expires_at: datetime,
    ) -> AccountCreatedOutbox | None:
        async with self._lock:
            event = next(
                (
                    candidate
                    for candidate in self._notification_outbox.values()
                    if candidate.account_id == account_id
                ),
                None,
            )
            if event is None:
                return None
            now = utc_now()
            if (
                event.status == NotificationOutboxStatus.PUBLISHING
                and event.lease_expires_at is not None
                and event.lease_expires_at > now
            ):
                return None
            if event.status not in {
                NotificationOutboxStatus.PENDING,
                NotificationOutboxStatus.PUBLISHING,
            }:
                return None
            event.status = NotificationOutboxStatus.PUBLISHING
            event.publish_attempt_count += 1
            event.lease_id = lease_id
            event.lease_expires_at = lease_expires_at
            event.last_error_code = None
            return event.model_copy(deep=True)

    async def mark_account_notification_published(
        self,
        *,
        event_id: str,
        lease_id: str,
        provider_message_id: str,
    ) -> bool:
        async with self._lock:
            event = self._notification_outbox.get(event_id)
            if (
                event is None
                or event.status != NotificationOutboxStatus.PUBLISHING
                or event.lease_id != lease_id
            ):
                return False
            event.status = NotificationOutboxStatus.PUBLISHED
            event.provider_message_id = provider_message_id
            event.published_at = utc_now()
            event.lease_id = None
            event.lease_expires_at = None
            return True

    async def release_account_notification_publish(
        self,
        *,
        event_id: str,
        lease_id: str,
        error_code: str,
    ) -> bool:
        async with self._lock:
            event = self._notification_outbox.get(event_id)
            if (
                event is None
                or event.status != NotificationOutboxStatus.PUBLISHING
                or event.lease_id != lease_id
            ):
                return False
            event.status = NotificationOutboxStatus.PENDING
            event.last_error_code = error_code
            event.lease_id = None
            event.lease_expires_at = None
            return True

    async def claim_account_notification_for_delivery(
        self,
        *,
        event_id: str,
        lease_id: str,
        lease_expires_at: datetime,
    ) -> AccountCreatedOutbox | None:
        async with self._lock:
            event = self._notification_outbox.get(event_id)
            if event is None or event.status == NotificationOutboxStatus.DELIVERED:
                return None
            now = utc_now()
            if (
                event.status == NotificationOutboxStatus.DELIVERING
                and event.lease_expires_at is not None
                and event.lease_expires_at > now
            ):
                return None
            if event.status not in {
                NotificationOutboxStatus.PUBLISHED,
                NotificationOutboxStatus.DELIVERING,
            }:
                return None
            event.status = NotificationOutboxStatus.DELIVERING
            event.delivery_attempt_count += 1
            event.lease_id = lease_id
            event.lease_expires_at = lease_expires_at
            event.last_error_code = None
            return event.model_copy(deep=True)

    async def mark_account_notification_delivered(
        self,
        *,
        event_id: str,
        lease_id: str,
        provider_delivery_id: str,
    ) -> bool:
        async with self._lock:
            event = self._notification_outbox.get(event_id)
            if (
                event is None
                or event.status != NotificationOutboxStatus.DELIVERING
                or event.lease_id != lease_id
            ):
                return False
            event.status = NotificationOutboxStatus.DELIVERED
            event.delivered_at = utc_now()
            event.provider_delivery_id = provider_delivery_id
            event.lease_id = None
            event.lease_expires_at = None
            return True

    async def release_account_notification_delivery(
        self,
        *,
        event_id: str,
        lease_id: str,
        error_code: str,
    ) -> bool:
        async with self._lock:
            event = self._notification_outbox.get(event_id)
            if (
                event is None
                or event.status != NotificationOutboxStatus.DELIVERING
                or event.lease_id != lease_id
            ):
                return False
            event.status = NotificationOutboxStatus.PUBLISHED
            event.last_error_code = error_code
            event.lease_id = None
            event.lease_expires_at = None
            return True

    async def account_for_owner(self, owner_user_id: str) -> Account:
        async with self._lock:
            account_id = self._account_by_owner.get(owner_user_id)
            if not account_id:
                raise AccountNotProvisioned
            return self._accounts[account_id].model_copy(deep=True)

    async def create_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        address: str,
        address_hash: str,
    ) -> InboundMailAddress:
        async with self._lock:
            account_id = self._account_by_owner.get(owner_user_id)
            if not account_id:
                raise AccountNotProvisioned
            existing = self._inbound_mail_addresses.get(account_id)
            if existing is not None:
                route = self._inbound_mail_routes.get(
                    sha256(existing.address.casefold().encode()).hexdigest()
                )
                if route is None or route.account_id != account_id:
                    raise InboundAddressStateConflict
                return existing.model_copy(deep=True)
            route = self._inbound_mail_routes.get(address_hash)
            if route is not None:
                if route.account_id == account_id:
                    raise InboundAddressStateConflict
                raise InboundAddressCollision
            created_at = utc_now()
            inbound_address = InboundMailAddress(
                account_id=account_id,
                address=address,
                created_at=created_at,
            )
            self._inbound_mail_addresses[account_id] = inbound_address
            self._inbound_mail_routes[address_hash] = InboundMailRoute(
                id=address_hash,
                account_id=account_id,
                created_at=created_at,
            )
            return inbound_address.model_copy(deep=True)

    async def seed_published_raw_mail(
        self,
        *,
        account_id: str,
        raw_mail_id: str,
        content_sha256: str,
    ) -> None:
        """Seed transport evidence in the in-memory adapter used by local workers/tests."""

        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            self._published_raw_mail[(account_id, raw_mail_id)] = content_sha256

    async def attach_purchase_document(
        self,
        candidate: PurchaseDocumentCandidate,
    ) -> PurchaseIdentityResult:
        async with self._lock:
            if candidate.account_id not in self._accounts:
                raise AccountNotProvisioned
            raw_content_sha256 = self._published_raw_mail.get(
                (candidate.account_id, candidate.raw_mail_id)
            )
            if raw_content_sha256 != candidate.raw_content_sha256:
                raise RawMailNotFound

            document_key = (candidate.account_id, candidate.raw_mail_id)
            existing_document = self._purchase_documents.get(document_key)
            if existing_document is not None:
                validate_purchase_document_retry(existing_document, candidate)
                purchase = self._purchases.get(
                    (candidate.account_id, existing_document.purchase_id)
                )
                if purchase is None or purchase.merchant != candidate.merchant:
                    raise PurchaseIdentityConflict
                for alias_id, kind, reference in purchase_identity_aliases(candidate):
                    alias = self._purchase_aliases.get((candidate.account_id, alias_id))
                    if alias is None:
                        raise PurchaseIdentityConflict
                    validate_purchase_identity_alias(
                        alias,
                        candidate=candidate,
                        alias_id=alias_id,
                        kind=kind,
                        reference=reference,
                    )
                    if alias.purchase_id != purchase.id:
                        raise PurchaseIdentityConflict
                return PurchaseIdentityResult(
                    purchase=purchase.model_copy(deep=True),
                    document=existing_document.model_copy(deep=True),
                    duplicate=True,
                )

            alias_specs = purchase_identity_aliases(candidate)
            existing_aliases = []
            for alias_id, kind, reference in alias_specs:
                existing_alias = self._purchase_aliases.get((candidate.account_id, alias_id))
                if existing_alias is not None:
                    validate_purchase_identity_alias(
                        existing_alias,
                        candidate=candidate,
                        alias_id=alias_id,
                        kind=kind,
                        reference=reference,
                    )
                    existing_aliases.append(existing_alias)
            purchase_ids = {alias.purchase_id for alias in existing_aliases}
            if len(purchase_ids) > 1:
                raise PurchaseIdentityConflict

            now = utc_now()
            if purchase_ids:
                purchase_id = purchase_ids.pop()
                purchase = self._purchases.get((candidate.account_id, purchase_id))
                if purchase is None or purchase.merchant != candidate.merchant:
                    raise PurchaseIdentityConflict
            else:
                purchase = Purchase(
                    id=str(uuid4()),
                    account_id=candidate.account_id,
                    merchant=candidate.merchant,
                    created_at=now,
                    updated_at=now,
                )

            for alias_id, kind, reference in alias_specs:
                existing_alias = self._purchase_aliases.get((candidate.account_id, alias_id))
                if existing_alias is not None:
                    validate_purchase_identity_alias(
                        existing_alias,
                        candidate=candidate,
                        alias_id=alias_id,
                        kind=kind,
                        reference=reference,
                    )
                    if existing_alias.purchase_id != purchase.id:
                        raise PurchaseIdentityConflict
                if existing_alias is None:
                    self._purchase_aliases[(candidate.account_id, alias_id)] = (
                        PurchaseIdentityAlias(
                            id=alias_id,
                            account_id=candidate.account_id,
                            purchase_id=purchase.id,
                            merchant=candidate.merchant,
                            kind=kind,
                            reference_hash=sha256(reference.encode()).hexdigest(),
                            created_at=now,
                        )
                    )

            revision_number = purchase.revision_count + 1
            document = PurchaseDocument(
                id=candidate.raw_mail_id,
                account_id=candidate.account_id,
                purchase_id=purchase.id,
                raw_mail_id=candidate.raw_mail_id,
                raw_content_sha256=candidate.raw_content_sha256,
                merchant=candidate.merchant,
                kind=candidate.kind,
                revision_number=revision_number,
                order_reference=candidate.order_reference,
                invoice_reference=candidate.invoice_reference,
                created_at=now,
            )
            purchase = purchase.model_copy(
                update={"revision_count": revision_number, "updated_at": now}
            )
            self._purchases[(candidate.account_id, purchase.id)] = purchase
            self._purchase_documents[document_key] = document
            return PurchaseIdentityResult(
                purchase=purchase.model_copy(deep=True),
                document=document.model_copy(deep=True),
                duplicate=False,
            )

    async def record_launch_mail_consent(
        self,
        *,
        owner_user_id: str,
        email_normalized: str,
        granted: bool,
        policy_version: str,
    ) -> LaunchMailConsent:
        account = await self.account_for_owner(owner_user_id)
        consent_id = sha256(
            f"{owner_user_id}\0{email_normalized}\0launch_mail\0{policy_version}\0{granted}".encode()
        ).hexdigest()
        async with self._lock:
            existing = self._launch_consents.get(consent_id)
            if existing:
                return existing.model_copy(deep=True)
            consent = LaunchMailConsent(
                id=consent_id,
                account_id=account.id,
                actor_user_id=owner_user_id,
                email_normalized=email_normalized,
                granted=granted,
                policy_version=policy_version,
            )
            self._launch_consents[consent.id] = consent
            return consent.model_copy(deep=True)

    async def join_waitlist(
        self,
        *,
        firebase_uid: str,
        email_normalized: str,
        policy_version: str,
    ) -> WaitlistEntry:
        email_hash = sha256(email_normalized.encode()).hexdigest()
        async with self._lock:
            if firebase_uid in self._account_by_owner:
                raise AccountAlreadyProvisioned
            existing = self._waitlist_by_email_hash.get(email_hash)
            if existing:
                if existing.firebase_uid != firebase_uid:
                    raise CrossAccountAccess
                return existing.model_copy(deep=True)
            public_account_count = sum(
                account.entitlement_mode == EntitlementMode.TRIAL
                for account in self._accounts.values()
            )
            if public_account_count < self._public_account_limit:
                raise WaitlistUnavailable
            entry = WaitlistEntry(
                id=email_hash,
                firebase_uid=firebase_uid,
                email_normalized=email_normalized,
                policy_version=policy_version,
            )
            self._waitlist_by_email_hash[email_hash] = entry
            return entry.model_copy(deep=True)

    async def issue_device_camera(
        self,
        *,
        owner_user_id: str,
        name: str,
        credential_hash: str,
        token_version: int,
    ) -> DeviceCamera:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            if credential_hash in self._device_credentials:
                raise DeviceCredentialCollision
            camera = DeviceCamera(
                id=str(uuid4()),
                account_id=account.id,
                name=name,
            )
            credential = DeviceCredentialRecord(
                credential_hash=credential_hash,
                account_id=account.id,
                camera_id=camera.id,
                token_version=token_version,
            )
            self._device_cameras[camera.id] = camera
            self._device_credentials[credential_hash] = credential
            return camera.model_copy(deep=True)

    async def authenticate_device(
        self,
        credential_hash: str,
    ) -> VerifiedDeviceIdentity:
        now = utc_now()
        async with self._lock:
            credential = self._device_credentials.get(credential_hash)
            if (
                credential is None
                or credential.status != DeviceCredentialStatus.ACTIVE
                or (credential.expires_at is not None and credential.expires_at <= now)
            ):
                raise InvalidDeviceCredential
            camera = self._device_cameras.get(credential.camera_id)
            account = self._accounts.get(credential.account_id)
            if (
                camera is None
                or camera.account_id != credential.account_id
                or camera.status != CameraStatus.ACTIVE
                or account is None
            ):
                raise InvalidDeviceCredential
            self._device_credentials[credential_hash] = credential.model_copy(
                update={"last_used_at": now}
            )
            return VerifiedDeviceIdentity(
                owner_user_id=account.owner_user_id,
                account_id=account.id,
                camera_id=camera.id,
            )

    async def revoke_device_camera(
        self,
        *,
        owner_user_id: str,
        camera_id: str,
    ) -> DeviceCamera:
        camera = await self._revoke_camera(
            owner_user_id=owner_user_id,
            camera_id=camera_id,
            expected_kind="device",
        )
        assert isinstance(camera, DeviceCamera)
        return camera

    async def revoke_camera(
        self,
        *,
        owner_user_id: str,
        camera_id: str,
    ) -> Camera:
        return await self._revoke_camera(
            owner_user_id=owner_user_id,
            camera_id=camera_id,
            expected_kind=None,
        )

    async def _revoke_camera(
        self,
        *,
        owner_user_id: str,
        camera_id: str,
        expected_kind: str | None,
    ) -> Camera:
        account = await self.account_for_owner(owner_user_id)
        now = utc_now()
        async with self._lock:
            camera: Camera | None = self._cameras.get(camera_id) or self._device_cameras.get(
                camera_id
            )
            if (
                camera is None
                or camera.account_id != account.id
                or (expected_kind is not None and camera.kind != expected_kind)
            ):
                raise CameraNotFound
            if camera.status == CameraStatus.REVOKED:
                return camera.model_copy(deep=True)
            revoked_camera = camera.model_copy(
                update={
                    "status": CameraStatus.REVOKED,
                    "revoked_at": now,
                },
                deep=True,
            )
            if isinstance(revoked_camera, BrowserCamera):
                self._cameras[camera_id] = revoked_camera
            else:
                self._device_cameras[camera_id] = revoked_camera
                for credential_hash, credential in self._device_credentials.items():
                    if credential.camera_id == camera_id:
                        self._device_credentials[credential_hash] = credential.model_copy(
                            update={
                                "status": DeviceCredentialStatus.REVOKED,
                                "revoked_at": now,
                            }
                        )
            return revoked_camera.model_copy(deep=True)

    async def device_camera_for_identity(
        self,
        *,
        account_id: str,
        camera_id: str,
    ) -> DeviceCamera:
        async with self._lock:
            camera = self._device_cameras.get(camera_id)
            if (
                camera is None
                or camera.account_id != account_id
                or camera.status != CameraStatus.ACTIVE
            ):
                raise CameraNotFound
            return camera.model_copy(deep=True)

    async def create_browser_camera(
        self,
        owner_user_id: str,
        name: str,
        client_instance_id: str,
    ) -> BrowserCamera:
        account = await self.account_for_owner(owner_user_id)
        instance_hash = sha256(client_instance_id.encode()).hexdigest()
        async with self._lock:
            instance_key = (account.id, instance_hash)
            existing_id = self._browser_camera_by_instance.get(instance_key)
            if existing_id:
                existing = self._cameras[existing_id]
                if existing.status == CameraStatus.ACTIVE and existing.name != name:
                    existing = existing.model_copy(update={"name": name}, deep=True)
                    self._cameras[existing.id] = existing
                return existing.model_copy(deep=True)
            camera = BrowserCamera(
                id=str(uuid4()),
                account_id=account.id,
                name=name,
                client_instance_id_hash=instance_hash,
            )
            self._cameras[camera.id] = camera
            self._browser_camera_by_instance[instance_key] = camera.id
            return camera.model_copy(deep=True)

    async def list_cameras(self, owner_user_id: str) -> list[Camera]:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            cameras: list[Camera] = [
                camera.model_copy(deep=True)
                for camera in (*self._cameras.values(), *self._device_cameras.values())
                if camera.account_id == account.id
            ]
        return sorted(cameras, key=lambda camera: (camera.created_at, camera.id))

    async def camera_for_owner(self, owner_user_id: str, camera_id: str) -> Camera:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            camera: Camera | None = self._cameras.get(camera_id) or self._device_cameras.get(
                camera_id
            )
            if camera is None or camera.status != CameraStatus.ACTIVE:
                raise CameraNotFound
            if camera.account_id != account.id:
                raise CrossAccountAccess
            return camera.model_copy(deep=True)

    async def reserve_capture(
        self,
        *,
        capture_id: str,
        account: Account,
        camera: BrowserCamera | DeviceCamera,
        idempotency_key: str,
        content_type: str,
        content_sha256: str,
        object_key: str,
        metadata: CaptureEnvelopeV1 | None = None,
    ) -> tuple[CaptureRecord, Account, bool]:
        validate_capture_scope(
            account=account,
            camera=camera,
            capture_id=capture_id,
            content_type=content_type,
            object_key=object_key,
            metadata=metadata,
        )
        async with self._lock:
            stored_account = self._accounts.get(account.id)
            if stored_account is None or stored_account.owner_user_id != account.owner_user_id:
                raise AccountNotProvisioned
            stored_camera: BrowserCamera | DeviceCamera | None
            if isinstance(camera, DeviceCamera):
                stored_camera = self._device_cameras.get(camera.id)
            else:
                stored_camera = self._cameras.get(camera.id)
            if stored_camera is None or stored_camera.status != CameraStatus.ACTIVE:
                raise CameraNotFound
            if stored_camera.account_id != account.id:
                raise CrossAccountAccess
            duplicate_id = self._capture_by_idempotency.get((account.id, idempotency_key))
            if duplicate_id:
                duplicate = self._captures[duplicate_id]
                if (
                    duplicate.camera_id != camera.id
                    or duplicate.content_type != content_type
                    or duplicate.content_sha256 != content_sha256
                    or duplicate.metadata != metadata
                ):
                    raise IdempotencyConflict
                return (
                    duplicate.model_copy(deep=True),
                    self._accounts[account.id].model_copy(deep=True),
                    False,
                )
            if (
                stored_account.entitlement_mode == EntitlementMode.TRIAL
                and stored_account.trial_image_limit is not None
                and stored_account.accepted_image_count >= stored_account.trial_image_limit
            ):
                raise TrialQuotaExhausted
            capture = CaptureRecord(
                id=capture_id,
                account_id=account.id,
                camera_id=camera.id,
                idempotency_key=idempotency_key,
                content_type=content_type,
                content_sha256=content_sha256,
                object_key=object_key,
                metadata=metadata,
            )
            stored_account.accepted_image_count += 1
            self._captures[capture.id] = capture
            self._capture_by_idempotency[(account.id, idempotency_key)] = capture.id
            return capture.model_copy(deep=True), stored_account.model_copy(deep=True), True

    async def cancel_capture(
        self,
        *,
        account_id: str,
        capture: CaptureRecord,
    ) -> None:
        if capture.account_id != account_id:
            raise CrossAccountAccess
        async with self._lock:
            stored = self._captures.get(capture.id)
            if not stored:
                return
            if stored.account_id != account_id:
                raise CaptureNotFound
            self._captures.pop(capture.id)
            self._capture_by_idempotency.pop((stored.account_id, stored.idempotency_key), None)
            meal_id = self._meal_by_capture.pop(stored.id, None)
            if meal_id:
                self._meals.pop(meal_id, None)
                self._meal_revisions.pop(meal_id, None)
                feedback_ids = [
                    feedback_id
                    for feedback_id, feedback in self._feedback.items()
                    if feedback.meal_id == meal_id
                ]
                for feedback_id in feedback_ids:
                    feedback = self._feedback.pop(feedback_id)
                    self._feedback_by_idempotency.pop(
                        (feedback.account_id, feedback.idempotency_key), None
                    )
                    self._revision_by_feedback.pop(feedback_id, None)
                question_id = self._question_by_meal.pop(meal_id, None)
                if question_id:
                    self._questions.pop(question_id, None)
            account = self._accounts[stored.account_id]
            account.accepted_image_count -= 1

    async def mark_stored(self, *, account_id: str, capture_id: str) -> None:
        async with self._lock:
            capture = self._captures.get(capture_id)
            if not capture or capture.account_id != account_id:
                raise CaptureNotFound
            if capture.status == CaptureStatus.ACCEPTED:
                capture.status = CaptureStatus.STORED
                self._enqueue_job_locked(
                    DurableJob(
                        id=capture_grouping_job_id(capture.id),
                        account_id=capture.account_id,
                        kind=JobKind.CAPTURE_GROUPING,
                        subject_id=capture.id,
                        subject_revision=1,
                    )
                )

    async def mark_processed(self, *, account_id: str, capture_id: str) -> None:
        async with self._lock:
            capture = self._captures.get(capture_id)
            if not capture or capture.account_id != account_id:
                raise CaptureNotFound
            capture.status = CaptureStatus.PROCESSED

    @staticmethod
    def _updated_job(job: DurableJob, **updates: object) -> DurableJob:
        return DurableJob.model_validate({**job.model_dump(mode="python"), **updates})

    def _enqueue_job_locked(self, job: DurableJob) -> DurableJob:
        validate_enqueueable_job(job)
        key = (job.account_id, job.id)
        existing = self._jobs.get(key)
        if existing is None:
            self._jobs[key] = job.model_copy(deep=True)
            return job.model_copy(deep=True)
        if existing.kind != job.kind or existing.subject_id != job.subject_id:
            raise JobIdentityConflict
        if job.subject_revision <= existing.subject_revision:
            return existing.model_copy(deep=True)
        replacement = job.model_copy(update={"created_at": existing.created_at}, deep=True)
        self._jobs[key] = replacement
        return replacement.model_copy(deep=True)

    async def enqueue_job(self, job: DurableJob) -> DurableJob:
        async with self._lock:
            return self._enqueue_job_locked(job)

    async def job_for_account(self, account_id: str, job_id: str) -> DurableJob | None:
        async with self._lock:
            job = self._jobs.get((account_id, job_id))
            return job.model_copy(deep=True) if job is not None else None

    async def claim_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> DurableJob | None:
        async with self._lock:
            now = utc_now()
            if lease_expires_at <= now:
                raise ValueError("Job leases must expire in the future")
            key = (account_id, job_id)
            job = self._jobs.get(key)
            if job is None or job.subject_revision != expected_subject_revision:
                return None
            if job.status == JobStatus.COMPLETED:
                return None
            if job.status == JobStatus.PENDING and job.available_at > now:
                return None
            if (
                job.status == JobStatus.LEASED
                and job.lease_expires_at is not None
                and job.lease_expires_at > now
            ):
                return None
            claimed = self._updated_job(
                job,
                status=JobStatus.LEASED,
                attempt_count=job.attempt_count + 1,
                lease_id=lease_id,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                last_error_code=None,
                last_error_message=None,
            )
            self._jobs[key] = claimed
            return claimed.model_copy(deep=True)

    async def complete_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
    ) -> bool:
        async with self._lock:
            key = (account_id, job_id)
            job = self._jobs.get(key)
            now = utc_now()
            if not self._job_has_active_lease(
                job,
                expected_subject_revision=expected_subject_revision,
                lease_id=lease_id,
                lease_owner=lease_owner,
                now=now,
            ):
                return False
            assert job is not None
            self._jobs[key] = self._updated_job(
                job,
                status=JobStatus.COMPLETED,
                lease_id=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=None,
                last_error_message=None,
                completed_at=now,
            )
            return True

    async def release_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        available_at: datetime,
        error_code: str,
        error_message: str,
    ) -> bool:
        async with self._lock:
            key = (account_id, job_id)
            job = self._jobs.get(key)
            now = utc_now()
            if not self._job_has_active_lease(
                job,
                expected_subject_revision=expected_subject_revision,
                lease_id=lease_id,
                lease_owner=lease_owner,
                now=now,
            ):
                return False
            assert job is not None
            self._jobs[key] = self._updated_job(
                job,
                status=JobStatus.PENDING,
                available_at=available_at,
                lease_id=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=error_code,
                last_error_message=error_message,
            )
            return True

    @staticmethod
    def _job_has_active_lease(
        job: DurableJob | None,
        *,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        now: datetime,
    ) -> bool:
        return bool(
            job is not None
            and job.status == JobStatus.LEASED
            and job.subject_revision == expected_subject_revision
            and job.lease_id == lease_id
            and job.lease_owner == lease_owner
            and job.lease_expires_at is not None
            and job.lease_expires_at > now
        )

    async def group_capture(
        self,
        *,
        account_id: str,
        capture_id: str,
        lease_id: str,
        lease_owner: str,
        policy: GroupingPolicy,
    ) -> CaptureGroupingResult | None:
        async with self._lock:
            now = utc_now()
            grouping_job_key = (account_id, capture_grouping_job_id(capture_id))
            grouping_job = self._jobs.get(grouping_job_key)
            if not self._job_has_active_lease(
                grouping_job,
                expected_subject_revision=1,
                lease_id=lease_id,
                lease_owner=lease_owner,
                now=now,
            ):
                return None
            capture = self._captures.get(capture_id)
            if (
                capture is None
                or capture.account_id != account_id
                or capture.status != CaptureStatus.STORED
            ):
                return None
            assert grouping_job is not None
            activity_at = capture_activity_time(capture)
            segment_id, source_key = segment_identity(capture)
            segment_key = (account_id, segment_id)
            segment = self._segments.get(segment_key)
            event_created = False
            segment_created = segment is None

            event: ActivityEvent | None = None
            if segment is not None:
                event = self._events.get((account_id, segment.event_id))
            if event is None:
                head_ids = {
                    self._event_head_by_source.get((account_id, capture.camera_id)),
                    self._event_head_by_source.get((account_id, ACCOUNT_EVENT_HEAD_ID)),
                }
                candidate_heads = [
                    head
                    for head_id in head_ids
                    if head_id is not None
                    and (head := self._events.get((account_id, head_id))) is not None
                    and head.first_capture_at - policy.reopen_window
                    <= activity_at
                    <= head.last_capture_at + policy.reopen_window
                ]
                if candidate_heads:
                    event = max(candidate_heads, key=lambda head: head.last_capture_at)
                else:
                    event_created = True
                    event = ActivityEvent(
                        id=str(uuid4()),
                        account_id=account_id,
                        camera_ids=[capture.camera_id],
                        first_capture_at=activity_at,
                        last_capture_at=activity_at,
                        capture_count=1,
                        grouping_policy_version=policy.version,
                        created_at=now,
                        updated_at=now,
                    )

            if not event_created:
                camera_ids = list(event.camera_ids)
                if capture.camera_id not in camera_ids:
                    camera_ids.append(capture.camera_id)
                event = ActivityEvent.model_validate(
                    {
                        **event.model_dump(mode="python"),
                        "status": ActivityEventStatus.OPEN,
                        "current_revision": event.current_revision + 1,
                        "camera_ids": camera_ids,
                        "first_capture_at": min(event.first_capture_at, activity_at),
                        "last_capture_at": max(event.last_capture_at, activity_at),
                        "capture_count": event.capture_count + 1,
                        "updated_at": now,
                    }
                )
            self._events[(account_id, event.id)] = event

            if segment is None:
                segment = ActivitySegment(
                    id=segment_id,
                    account_id=account_id,
                    event_id=event.id,
                    camera_id=capture.camera_id,
                    source_key=source_key,
                    first_capture_at=activity_at,
                    last_capture_at=activity_at,
                    capture_count=1,
                    created_at=now,
                )
            else:
                segment = ActivitySegment.model_validate(
                    {
                        **segment.model_dump(mode="python"),
                        "first_capture_at": min(segment.first_capture_at, activity_at),
                        "last_capture_at": max(segment.last_capture_at, activity_at),
                        "capture_count": segment.capture_count + 1,
                    }
                )
            self._segments[segment_key] = segment
            current_head_id = self._event_head_by_source.get((account_id, capture.camera_id))
            current_head = (
                self._events.get((account_id, current_head_id))
                if current_head_id is not None
                else None
            )
            if current_head is None or event.last_capture_at >= current_head.last_capture_at:
                self._event_head_by_source[(account_id, capture.camera_id)] = event.id
            account_head_id = self._event_head_by_source.get((account_id, ACCOUNT_EVENT_HEAD_ID))
            account_head = (
                self._events.get((account_id, account_head_id))
                if account_head_id is not None
                else None
            )
            if account_head is None or event.last_capture_at >= account_head.last_capture_at:
                self._event_head_by_source[(account_id, ACCOUNT_EVENT_HEAD_ID)] = event.id

            self._captures[capture.id] = capture.model_copy(
                update={"segment_id": segment.id, "event_id": event.id},
                deep=True,
            )
            inference_job = self._enqueue_job_locked(
                DurableJob(
                    id=event_inference_job_id(event.id),
                    account_id=account_id,
                    kind=JobKind.EVENT_INFERENCE,
                    subject_id=event.id,
                    subject_revision=event.current_revision,
                    available_at=event.last_capture_at + policy.quiet_after,
                    created_at=now,
                )
            )
            self._jobs[grouping_job_key] = self._updated_job(
                grouping_job,
                status=JobStatus.COMPLETED,
                lease_id=None,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=now,
            )
            return CaptureGroupingResult(
                event=event.model_copy(deep=True),
                segment=segment.model_copy(deep=True),
                inference_job=inference_job,
                event_created=event_created,
                segment_created=segment_created,
            )

    async def event_evidence_for_account(
        self,
        *,
        account_id: str,
        event_id: str,
    ) -> tuple[ActivityEvent, list[CaptureRecord]]:
        async with self._lock:
            event = self._events.get((account_id, event_id))
            if event is None:
                raise ActivityEventNotFound
            captures = [
                capture.model_copy(deep=True)
                for capture in self._captures.values()
                if capture.account_id == account_id and capture.event_id == event_id
            ]
            if len(captures) != event.capture_count:
                raise ValueError("Activity event evidence is incomplete")
            return event.model_copy(deep=True), sorted(captures, key=capture_evidence_order)

    async def publish_event_inference(
        self,
        *,
        account_id: str,
        event_id: str,
        expected_event_revision: int,
        lease_id: str,
        lease_owner: str,
        hypothesis: ActivityMealInferenceV1,
    ) -> MealEntry | None:
        async with self._lock:
            now = utc_now()
            job_key = (account_id, event_inference_job_id(event_id))
            job = self._jobs.get(job_key)
            if not self._job_has_active_lease(
                job,
                expected_subject_revision=expected_event_revision,
                lease_id=lease_id,
                lease_owner=lease_owner,
                now=now,
            ):
                return None
            event_key = (account_id, event_id)
            event = self._events.get(event_key)
            if (
                event is None
                or event.current_revision != expected_event_revision
                or event.status != ActivityEventStatus.OPEN
            ):
                return None
            captures = sorted(
                [
                    capture
                    for capture in self._captures.values()
                    if capture.account_id == account_id and capture.event_id == event_id
                ],
                key=capture_evidence_order,
            )
            if len(captures) != event.capture_count:
                raise ValueError("Activity event evidence is incomplete")

            meal_id = event.meal_id or event.id
            existing_meal = self._meals.get(meal_id)
            if existing_meal is not None and (
                existing_meal.account_id != account_id or existing_meal.event_id != event_id
            ):
                raise CrossAccountAccess
            revision_number = (
                existing_meal.revision_number + 1 if existing_meal is not None else 1
            )
            created_at = existing_meal.created_at if existing_meal is not None else now
            meal = materialize_activity_hypothesis(
                event=event,
                captures=captures,
                hypothesis=hypothesis,
                meal_id=meal_id,
                revision_number=revision_number,
                created_at=created_at,
            )
            revision = MealRevision(
                id=str(uuid4()),
                account_id=account_id,
                meal_id=meal.id,
                number=revision_number,
                status=meal.status,
                inference=inference_from_meal(meal),
                activity_hypothesis=hypothesis,
                source=MealRevisionSource.INFERENCE,
                created_at=now,
            )
            question = event_question_from_hypothesis(
                meal=meal,
                revision=revision,
                hypothesis=hypothesis,
                created_at=now,
            )
            previous_question_id = self._question_by_meal.get(meal.id)
            previous_question = (
                self._questions.get(previous_question_id)
                if previous_question_id is not None
                else None
            )
            if previous_question is not None and previous_question.status == QuestionStatus.OPEN:
                previous_question.status = QuestionStatus.SUPERSEDED
                previous_question.superseded_by_question_id = question.id if question else None
                previous_question.superseded_at = now
            if question is not None:
                self._questions[question.id] = question
                self._question_by_meal[meal.id] = question.id
            else:
                self._question_by_meal.pop(meal.id, None)

            self._meals[meal.id] = meal
            self._meal_by_capture[meal.capture_id] = meal.id
            self._meal_revisions.setdefault(meal.id, []).append(revision)
            self._events[event_key] = event.model_copy(
                update={
                    "status": ActivityEventStatus.INFERRED,
                    "meal_id": meal.id,
                    "updated_at": now,
                },
                deep=True,
            )
            for capture in captures:
                self._captures[capture.id] = capture.model_copy(
                    update={"status": CaptureStatus.PROCESSED},
                    deep=True,
                )
            assert job is not None
            self._jobs[job_key] = self._updated_job(
                job,
                status=JobStatus.COMPLETED,
                lease_id=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=None,
                last_error_message=None,
                completed_at=now,
            )
            return meal.model_copy(deep=True)

    async def reserve_model_spend(
        self,
        reservation: ModelSpendReservation,
    ) -> ModelSpendReservation:
        async with self._lock:
            if reservation.account_id not in self._accounts:
                raise AccountNotProvisioned
            existing = self._model_spend_reservations.get(reservation.id)
            if existing is not None:
                if existing.model_dump(exclude={"created_at"}) != reservation.model_dump(
                    exclude={"created_at"}
                ):
                    raise ModelSpendReservationConflict
                return existing.model_copy(deep=True)
            proposed_total = (
                self._model_spend_reserved_dkk_micros
                + reservation.reserved_dkk_micros
            )
            if proposed_total > self._model_spend_limit_dkk_micros:
                raise ModelSpendLimitExceeded
            stored = reservation.model_copy(deep=True)
            self._model_spend_reservations[stored.id] = stored
            self._model_spend_reserved_dkk_micros = proposed_total
            return stored.model_copy(deep=True)

    async def model_usage_for_reservation(
        self,
        *,
        account_id: str,
        reservation_id: str,
    ) -> ModelUsageRecord | None:
        async with self._lock:
            usage = self._model_usage.get(reservation_id)
            if usage is None or usage.account_id != account_id:
                return None
            return usage.model_copy(deep=True)

    async def record_model_usage(self, usage: ModelUsageRecord) -> ModelUsageRecord:
        async with self._lock:
            reservation = self._model_spend_reservations.get(usage.reservation_id)
            if reservation is None or reservation.account_id != usage.account_id:
                raise ModelUsageConflict
            if (
                usage.id != reservation.id
                or usage.event_id != reservation.event_id
                or usage.reserved_dkk_micros != reservation.reserved_dkk_micros
                or usage.model != reservation.model
                or usage.region != reservation.region
                or usage.purpose != reservation.purpose
                or usage.prompt_version != reservation.prompt_version
                or usage.retry_attempt != reservation.retry_attempt
                or usage.evaluation != reservation.evaluation
            ):
                raise ModelUsageConflict
            if usage.actual_dkk_micros > reservation.reserved_dkk_micros:
                raise ModelUsageExceedsReservation
            existing = self._model_usage.get(usage.reservation_id)
            if existing is not None:
                if existing.model_dump(exclude={"created_at"}) != usage.model_dump(
                    exclude={"created_at"}
                ):
                    raise ModelUsageConflict
                return existing.model_copy(deep=True)
            stored = usage.model_copy(deep=True)
            self._model_usage[stored.reservation_id] = stored
            self._model_spend_actual_dkk_micros += stored.actual_dkk_micros
            return stored.model_copy(deep=True)

    async def save_meal(self, *, account_id: str, meal: MealEntry) -> MealEntry:
        if meal.account_id != account_id:
            raise CrossAccountAccess
        async with self._lock:
            capture = self._captures.get(meal.capture_id)
            if capture is None or capture.account_id != account_id:
                raise CaptureNotFound
            existing_id = self._meal_by_capture.get(meal.capture_id)
            if existing_id:
                existing = self._meals[existing_id]
                if existing.account_id != account_id:
                    raise MealNotFound
                return existing.model_copy(deep=True)
            self._meals[meal.id] = meal
            self._meal_by_capture[meal.capture_id] = meal.id
            self._meal_revisions[meal.id] = [
                MealRevision(
                    id=str(uuid4()),
                    account_id=meal.account_id,
                    meal_id=meal.id,
                    number=1,
                    status=meal.status,
                    inference=self._inference_from_meal(meal),
                    source=MealRevisionSource.INFERENCE,
                    created_at=meal.created_at,
                )
            ]
            return meal.model_copy(deep=True)

    async def open_question(
        self,
        *,
        account_id: str,
        meal: MealEntry,
        prompt: str,
        reason: str,
    ) -> ClarificationQuestion:
        if meal.account_id != account_id:
            raise CrossAccountAccess
        validate_focused_question_prompt(prompt)
        async with self._lock:
            stored_meal = self._meals.get(meal.id)
            if stored_meal is None or stored_meal.account_id != account_id:
                raise MealNotFound
            existing_id = self._question_by_meal.get(meal.id)
            if existing_id:
                existing = self._questions[existing_id]
                if existing.account_id != account_id:
                    raise QuestionNotFound
                return existing.model_copy(deep=True)
            choices = list(dict.fromkeys([stored_meal.title, *stored_meal.alternatives]))[:8]
            if len(choices) < 2:
                raise ValueError("focused event questions require at least two concrete choices")
            revision = self._meal_revisions[meal.id][-1]
            question = ClarificationQuestion(
                id=meal.id,
                account_id=account_id,
                kind=QuestionKind.EVENT_CLARIFICATION,
                meal_id=stored_meal.id,
                event_id=stored_meal.event_id,
                prompt=prompt,
                reason=reason,
                evidence=[
                    QuestionEvidenceReference(
                        kind=QuestionEvidenceKind.MEAL_REVISION,
                        id=revision.id,
                    )
                ],
                choices=choices,
                source_revision_number=revision.number,
            )
            self._questions[question.id] = question
            self._question_by_meal[meal.id] = question.id
            return question.model_copy(deep=True)

    async def open_pattern_question(
        self,
        *,
        account_id: str,
        prompt: str,
        reason: str,
        tentative_claim: str,
        evidence: list[QuestionEvidenceReference],
        supersedes_question_id: str | None = None,
    ) -> ClarificationQuestion:
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            question_id = pattern_question_id(account_id, tentative_claim)
            existing = self._questions.get(question_id)
            if existing is not None:
                if existing.account_id != account_id:
                    raise QuestionNotFound
                return existing.model_copy(deep=True)
            superseded = None
            if supersedes_question_id is not None:
                superseded = self._questions.get(supersedes_question_id)
                if (
                    superseded is None
                    or superseded.account_id != account_id
                    or superseded.kind != QuestionKind.PATTERN_HYPOTHESIS
                ):
                    raise QuestionNotFound
                if superseded.status != QuestionStatus.OPEN:
                    raise QuestionSuperseded
            now = utc_now()
            question = ClarificationQuestion(
                id=question_id,
                account_id=account_id,
                kind=QuestionKind.PATTERN_HYPOTHESIS,
                prompt=prompt,
                reason=reason,
                evidence=evidence,
                tentative_claim=tentative_claim,
                created_at=now,
            )
            if superseded is not None:
                superseded.status = QuestionStatus.SUPERSEDED
                superseded.superseded_by_question_id = question.id
                superseded.superseded_at = now
            self._questions[question.id] = question
            return question.model_copy(deep=True)

    async def list_meals(self, owner_user_id: str) -> list[MealEntry]:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            meals: Iterable[MealEntry] = (
                meal for meal in self._meals.values() if meal.account_id == account.id
            )
            return [
                meal.model_copy(deep=True)
                for meal in sorted(meals, key=lambda item: item.created_at, reverse=True)
            ]

    async def meal_for_owner(self, owner_user_id: str, meal_id: str) -> MealEntry:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            meal = self._meals.get(meal_id)
            if not meal:
                raise MealNotFound
            if meal.account_id != account.id:
                raise CrossAccountAccess
            return meal.model_copy(deep=True)

    async def list_meal_revisions(
        self,
        owner_user_id: str,
        meal_id: str,
    ) -> list[MealRevision]:
        await self.meal_for_owner(owner_user_id, meal_id)
        async with self._lock:
            return [revision.model_copy(deep=True) for revision in self._meal_revisions[meal_id]]

    async def record_knowledge_revision(
        self,
        *,
        account_id: str,
        topic_key: str,
        expected_revision_number: int | None,
        draft: KnowledgeRevisionDraft,
        idempotency_key: str,
    ) -> KnowledgeRevisionResult:
        request_hash = knowledge_revision_request_hash(
            topic_key=topic_key,
            expected_revision_number=expected_revision_number,
            draft=draft,
        )
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            duplicate = self._knowledge_revision_requests.get(
                (account_id, idempotency_key)
            )
            if duplicate is not None:
                stored_hash, result = duplicate
                if stored_hash != request_hash:
                    raise IdempotencyConflict
                return result.model_copy(deep=True)

            normalized_topic = normalize_knowledge_topic(topic_key)
            page_id = knowledge_page_id(account_id, normalized_topic)
            revisions = self._knowledge_revisions.get(page_id, [])
            previous = revisions[-1] if revisions else None
            validate_knowledge_revision(
                previous=previous,
                expected_revision_number=expected_revision_number,
                draft=draft,
            )
            created_at = utc_now()
            revision = KnowledgeRevision(
                **draft.model_dump(),
                id=sha256(idempotency_key.encode()).hexdigest(),
                account_id=account_id,
                page_id=page_id,
                number=1 if previous is None else previous.number + 1,
                base_revision_number=(previous.number if previous is not None else None),
                previous_revision_id=(previous.id if previous is not None else None),
                created_at=created_at,
            )
            existing_page = self._knowledge_pages.get(page_id)
            page = materialize_knowledge_page(
                topic_key=normalized_topic,
                revision=revision,
                created_at=(existing_page.created_at if existing_page else created_at),
            )
            result = KnowledgeRevisionResult(page=page, revision=revision)
            self._knowledge_pages[page_id] = page
            self._knowledge_revisions.setdefault(page_id, []).append(revision)
            self._knowledge_revision_requests[(account_id, idempotency_key)] = (
                request_hash,
                result,
            )
            return result.model_copy(deep=True)

    async def knowledge_page_for_owner(
        self,
        owner_user_id: str,
        page_id: str,
    ) -> KnowledgePage:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            page = self._knowledge_pages.get(page_id)
            if page is None or page.account_id != account.id:
                raise KnowledgePageNotFound
            return page.model_copy(deep=True)

    async def list_knowledge_revisions(
        self,
        owner_user_id: str,
        page_id: str,
    ) -> list[KnowledgeRevision]:
        await self.knowledge_page_for_owner(owner_user_id, page_id)
        async with self._lock:
            return [
                revision.model_copy(deep=True)
                for revision in self._knowledge_revisions[page_id]
            ]

    async def record_meal_feedback(
        self,
        *,
        owner_user_id: str,
        meal_id: str,
        request: MealFeedbackRequest,
        idempotency_key: str,
    ) -> MealFeedbackResult:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            meal = self._owned_meal(account.id, meal_id)
            return self._record_feedback_locked(
                account_id=account.id,
                meal=meal,
                request=request,
                idempotency_key=idempotency_key,
            )

    async def list_questions(
        self,
        owner_user_id: str,
        *,
        question_status: QuestionStatus | None = None,
    ) -> list[ClarificationQuestion]:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            questions: Iterable[ClarificationQuestion] = (
                question
                for question in self._questions.values()
                if question.account_id == account.id
                and (question_status is None or question.status == question_status)
            )
            return [
                question.model_copy(deep=True)
                for question in sorted(
                    questions,
                    key=lambda item: item.created_at,
                    reverse=True,
                )
            ]

    async def answer_question(
        self,
        *,
        owner_user_id: str,
        question_id: str,
        request: QuestionAnswerRequest,
        idempotency_key: str,
    ) -> QuestionAnswerResult:
        result = await self.respond_to_question(
            owner_user_id=owner_user_id,
            question_id=question_id,
            request=QuestionResponseRequest(
                kind=QuestionResponseKind.CORRECT,
                correction=request.answer,
                explanation=request.learning_tip,
            ),
            idempotency_key=idempotency_key,
        )
        if result.feedback is None or result.revision is None:
            raise ValueError("legacy event answer did not create a meal revision")
        return QuestionAnswerResult(
            question=result.question,
            feedback=result.feedback,
            revision=result.revision,
        )

    async def respond_to_question(
        self,
        *,
        owner_user_id: str,
        question_id: str,
        request: QuestionResponseRequest,
        idempotency_key: str,
    ) -> QuestionResponseResult:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            question = self._questions.get(question_id)
            if not question or question.account_id != account.id:
                raise QuestionNotFound
            duplicate_id = self._question_response_by_idempotency.get(
                (account.id, idempotency_key)
            )
            if duplicate_id is not None:
                response = self._question_responses[duplicate_id]
                if (
                    response.question_id != question.id
                    or response.kind != request.kind
                    or response.correction != request.correction
                    or response.explanation != request.explanation
                ):
                    raise IdempotencyConflict
                feedback = (
                    self._feedback.get(response.feedback_id)
                    if response.feedback_id is not None
                    else None
                )
                revision = (
                    self._revision_by_feedback.get(response.feedback_id)
                    if response.feedback_id is not None
                    else None
                )
                return QuestionResponseResult(
                    question=question.model_copy(deep=True),
                    response=response.model_copy(deep=True),
                    feedback=feedback.model_copy(deep=True) if feedback else None,
                    revision=revision.model_copy(deep=True) if revision else None,
                )
            if question.status == QuestionStatus.SUPERSEDED:
                raise QuestionSuperseded
            if question.status != QuestionStatus.OPEN:
                raise QuestionAlreadyAnswered

            feedback_result = None
            if question.kind == QuestionKind.EVENT_CLARIFICATION and request.kind in {
                QuestionResponseKind.CONFIRM,
                QuestionResponseKind.CORRECT,
            }:
                if question.meal_id is None:
                    raise ValueError("event question is missing its meal")
                meal = self._owned_meal(account.id, question.meal_id)
                feedback_request = MealFeedbackRequest(
                    kind=(
                        MealFeedbackKind.CONFIRM
                        if request.kind == QuestionResponseKind.CONFIRM
                        else MealFeedbackKind.CORRECT
                    ),
                    actual_meal=(
                        request.correction
                        if request.kind == QuestionResponseKind.CORRECT
                        else None
                    ),
                    explanation=(
                        request.explanation
                        if request.kind == QuestionResponseKind.CORRECT
                        else None
                    ),
                )
                feedback_result = self._record_feedback_locked(
                    account_id=account.id,
                    meal=meal,
                    request=feedback_request,
                    idempotency_key=idempotency_key,
                    question_id=question.id,
                )

            response = QuestionResponse(
                id=sha256(idempotency_key.encode()).hexdigest(),
                account_id=account.id,
                question_id=question.id,
                kind=request.kind,
                correction=request.correction,
                explanation=request.explanation,
                idempotency_key=idempotency_key,
                feedback_id=(
                    feedback_result.feedback.id if feedback_result is not None else None
                ),
            )
            question.status = QuestionStatus.ANSWERED
            question.response_kind = request.kind
            question.response_id = response.id
            question.answer = request.correction or request.kind.value
            question.learning_tip = request.explanation
            question.answered_at = response.created_at
            self._question_responses[response.id] = response
            self._question_response_by_idempotency[(account.id, idempotency_key)] = response.id
            return QuestionResponseResult(
                question=question.model_copy(deep=True),
                response=response.model_copy(deep=True),
                feedback=(feedback_result.feedback if feedback_result else None),
                revision=(feedback_result.revision if feedback_result else None),
            )

    async def capture_for_owner(self, owner_user_id: str, capture_id: str) -> CaptureRecord:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            capture = self._captures.get(capture_id)
            if not capture:
                raise CaptureNotFound
            if capture.account_id != account.id:
                raise CrossAccountAccess
            return capture.model_copy(deep=True)

    def _owned_meal(self, account_id: str, meal_id: str) -> MealEntry:
        meal = self._meals.get(meal_id)
        if not meal:
            raise MealNotFound
        if meal.account_id != account_id:
            raise CrossAccountAccess
        return meal

    def _record_feedback_locked(
        self,
        *,
        account_id: str,
        meal: MealEntry,
        request: MealFeedbackRequest,
        idempotency_key: str,
        question_id: str | None = None,
    ) -> MealFeedbackResult:
        duplicate_id = self._feedback_by_idempotency.get((account_id, idempotency_key))
        if duplicate_id:
            feedback = self._feedback[duplicate_id]
            if (
                feedback.meal_id != meal.id
                or feedback.kind != request.kind
                or feedback.actual_meal != request.actual_meal
                or feedback.explanation != request.explanation
                or feedback.correction != request.correction
                or feedback.base_revision_number != request.base_revision_number
                or feedback.question_id != question_id
            ):
                raise IdempotencyConflict
            return MealFeedbackResult(
                feedback=feedback.model_copy(deep=True),
                revision=self._revision_by_feedback[feedback.id].model_copy(deep=True),
            )

        feedback = MealFeedback(
            id=str(uuid4()),
            account_id=account_id,
            meal_id=meal.id,
            kind=request.kind,
            actual_meal=request.actual_meal,
            explanation=request.explanation,
            correction=request.correction,
            base_revision_number=request.base_revision_number,
            idempotency_key=idempotency_key,
            question_id=question_id,
        )
        inference, meal_status = self._revised_inference(meal, request)
        revision = MealRevision(
            id=str(uuid4()),
            account_id=account_id,
            meal_id=meal.id,
            number=meal.revision_number + 1,
            status=meal_status,
            inference=inference,
            activity_hypothesis=meal.activity_hypothesis,
            source=MealRevisionSource.USER_FEEDBACK,
            feedback_id=feedback.id,
            base_revision_number=request.base_revision_number,
            correction=request.correction,
        )
        updated_meal = MealEntry(
            **inference.model_dump(),
            id=meal.id,
            account_id=meal.account_id,
            capture_id=meal.capture_id,
            event_id=meal.event_id,
            occurred_at=meal.occurred_at,
            activity_hypothesis=meal.activity_hypothesis,
            status=meal_status,
            revision_number=revision.number,
            created_at=meal.created_at,
        )
        self._feedback[feedback.id] = feedback
        self._feedback_by_idempotency[(account_id, idempotency_key)] = feedback.id
        self._revision_by_feedback[feedback.id] = revision
        self._meal_revisions[meal.id].append(revision)
        self._meals[meal.id] = updated_meal
        return MealFeedbackResult(
            feedback=feedback.model_copy(deep=True),
            revision=revision.model_copy(deep=True),
        )

    @staticmethod
    def _inference_from_meal(meal: MealEntry) -> MealInference:
        return inference_from_meal(meal)

    @classmethod
    def _revised_inference(
        cls,
        meal: MealEntry,
        request: MealFeedbackRequest,
    ) -> tuple[MealInference, MealStatus]:
        return revised_inference(meal, request)


def inference_from_meal(meal: MealEntry) -> MealInference:
    return MealInference(**meal.model_dump(include=set(MealInference.model_fields)))


def revised_inference(
    meal: MealEntry,
    request: MealFeedbackRequest,
) -> tuple[MealInference, MealStatus]:
    if request.kind == MealFeedbackKind.CONFIRM:
        return inference_from_meal(meal), MealStatus.CONFIRMED

    if request.correction is not None:
        if request.base_revision_number != meal.revision_number:
            raise MealRevisionConflict
        return targeted_correction_inference(meal, request)

    alternatives = list(dict.fromkeys([meal.title, *meal.alternatives]))
    if request.actual_meal:
        rationale = (
            f"User correction: {request.explanation}"
            if request.explanation
            else f"The account owner identified this meal as {request.actual_meal}."
        )
        return (
            MealInference(
                title=request.actual_meal,
                confidence=Confidence.CONFIDENT,
                components=[
                    MealComponent(
                        name=request.actual_meal,
                        ingredients=[],
                        preparation_methods=[],
                    )
                ],
                observations=meal.observations,
                alternatives=[
                    alternative
                    for alternative in alternatives
                    if alternative != request.actual_meal
                ],
                rationale=rationale,
            ),
            MealStatus.CORRECTED,
        )

    return (
        MealInference(
            title="Unresolved meal",
            confidence=Confidence.UNCERTAIN,
            components=[],
            observations=meal.observations,
            alternatives=alternatives,
            rationale=(
                f"User correction: {request.explanation}"
                if request.explanation
                else "The account owner marked the inference as wrong without providing a "
                "replacement."
            ),
        ),
        MealStatus.CONTRADICTED,
    )


def targeted_correction_inference(
    meal: MealEntry,
    request: MealFeedbackRequest,
) -> tuple[MealInference, MealStatus]:
    correction = request.correction
    if correction is None:
        raise ValueError("targeted correction is required")

    components = [component.model_copy(deep=True) for component in meal.components]
    title = meal.title
    confidence = meal.confidence
    if isinstance(correction, WholeMealCorrection):
        title = correction.title
        confidence = Confidence.CONFIDENT
        components = (
            [component.model_copy(deep=True) for component in correction.components]
            if correction.components is not None
            else [
                MealComponent(
                    name=correction.title,
                    ingredients=[],
                    preparation_methods=[],
                )
            ]
        )
    elif isinstance(correction, ComponentCorrection):
        if correction.component_index >= len(components):
            raise InvalidMealCorrectionTarget
        old_component = components[correction.component_index]
        components[correction.component_index] = correction.replacement.model_copy(deep=True)
        if len(components) == 1 and title == old_component.name:
            title = correction.replacement.name
    elif isinstance(correction, IngredientCorrection):
        component = _correction_component(components, correction.component_index)
        if correction.ingredient_index >= len(component.ingredients):
            raise InvalidMealCorrectionTarget
        component.ingredients[correction.ingredient_index] = correction.replacement
    elif isinstance(correction, PreparationMethodCorrection):
        component = _correction_component(components, correction.component_index)
        if correction.preparation_method_index >= len(component.preparation_methods):
            raise InvalidMealCorrectionTarget
        component.preparation_methods[correction.preparation_method_index] = (
            correction.replacement
        )
    else:  # pragma: no cover - the discriminated model union is exhaustive
        raise InvalidMealCorrectionTarget

    correction_note = (
        f"User correction ({correction.scope}): {request.explanation}"
        if request.explanation
        else f"The account owner corrected the {correction.scope.replace('_', ' ')}."
    )
    return (
        MealInference(
            title=title,
            confidence=confidence,
            components=components,
            observations=list(meal.observations),
            alternatives=list(meal.alternatives),
            rationale=f"{meal.rationale}\n\n{correction_note}",
        ),
        MealStatus.CORRECTED,
    )


def _correction_component(
    components: list[MealComponent],
    component_index: int,
) -> MealComponent:
    if component_index >= len(components):
        raise InvalidMealCorrectionTarget
    return components[component_index]
