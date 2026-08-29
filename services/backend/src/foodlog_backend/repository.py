import asyncio
import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from hashlib import sha256
from math import ceil
from typing import Protocol
from unicodedata import normalize as unicode_normalize
from uuid import uuid4

from .audit import build_audit_event
from .errors import (
    AccountAlreadyProvisioned,
    AccountCapacityReached,
    AccountCapacityStateConflict,
    AccountExportAlreadyActive,
    AccountExportNotFound,
    AccountExportRateLimited,
    AccountNotProvisioned,
    ActivityEventNotFound,
    AiTraceConflict,
    AiTraceNotFound,
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
    InvalidMealFeedbackTransition,
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
    PurchaseNormalizationConflict,
    PurchaseNotFound,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    QuestionSuperseded,
    RawMailAuthenticationConflict,
    RawMailNotFound,
    RawMailProcessingConflict,
    TrialQuotaExhausted,
    UserContextNoteNotFound,
    WaitlistEntryNotFound,
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
    AccountCapacityAction,
    AccountCapacityOperation,
    AccountCapacityReason,
    AccountCreatedOutbox,
    AccountExport,
    AccountExportStatus,
    ActivityEvent,
    ActivityEventStatus,
    ActivitySegment,
    AiTraceRecord,
    AuditAction,
    AuditActorKind,
    AuditEvent,
    AuditPurpose,
    AuditSource,
    BrowserCamera,
    Camera,
    CameraStatus,
    CaptureEnvelopeV1,
    CaptureRecord,
    CaptureStatus,
    ClarificationQuestion,
    ComponentCorrection,
    Confidence,
    ConsentPreferences,
    DeviceCamera,
    DeviceCredentialRecord,
    DeviceCredentialStatus,
    DurableJob,
    EntitlementMode,
    InboundMailAddress,
    InboundMailAddressStatus,
    InboundMailRoute,
    IngredientCorrection,
    JobKind,
    JobStatus,
    KnowledgeClaim,
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
    MealFeedbackView,
    MealInference,
    MealRevision,
    MealRevisionSource,
    MealStatus,
    ModelSpendReservation,
    ModelUsageRecord,
    NotificationOutboxStatus,
    ParsedPurchaseDocument,
    PatternEvidenceExample,
    PreparationMethodCorrection,
    Purchase,
    PurchaseCharge,
    PurchaseDocument,
    PurchaseDocumentCandidate,
    PurchaseDocumentKind,
    PurchaseDocumentNormalization,
    PurchaseEvidenceBundle,
    PurchaseEvidenceOrigin,
    PurchaseIdentityAlias,
    PurchaseIdentityResult,
    PurchaseItem,
    PurchaseNormalizationResult,
    PurchaseReconciliation,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionEvidenceKind,
    QuestionEvidenceReference,
    QuestionKind,
    QuestionResponse,
    QuestionResponseKind,
    QuestionResponseRequest,
    QuestionResponseResult,
    QuestionResponseView,
    QuestionStatus,
    RawMailAuthentication,
    RawMailAuthenticationOutcome,
    RawMailProcessingDisposition,
    UserContextNote,
    UserContextNoteCreate,
    UserContextNoteStatus,
    VerifiedDeviceIdentity,
    WaitlistEntry,
    WholeMealCorrection,
    account_export_id,
    account_export_job_id,
    capture_grouping_job_id,
    event_inference_job_id,
    utc_now,
)
from .purchase_normalization import (
    materialize_purchase_document_normalization,
    reconcile_purchase_items,
)

PATTERN_RESURFACE_MINIMUM_NEW_SUPPORT = 2


def validate_ai_trace_usage(trace: AiTraceRecord, usage: ModelUsageRecord) -> None:
    if (
        trace.reservation_id != usage.reservation_id
        or trace.account_id != usage.account_id
        or trace.event_id != usage.event_id
        or trace.status != usage.outcome
        or trace.model != usage.model
        or trace.model_version != usage.model_version
        or trace.provider_invocation_id != usage.invocation_id
        or trace.region != usage.region
        or trace.prompt_version != usage.prompt_version
        or trace.purpose != usage.purpose
        or trace.retry_attempt != usage.retry_attempt
        or trace.evaluation != usage.evaluation
        or trace.prompt_tokens != usage.prompt_tokens
        or trace.response_tokens != usage.response_tokens
        or trace.thinking_tokens != usage.thinking_tokens
        or trace.total_tokens != usage.total_tokens
        or trace.actual_dkk_micros != usage.actual_dkk_micros
        or trace.error_code != usage.error_code
    ):
        raise AiTraceConflict


def event_question_id(meal_id: str, revision_number: int) -> str:
    identity = f"event-question-v1:{meal_id}:{revision_number}"
    return sha256(identity.encode()).hexdigest()


def pattern_question_id(account_id: str, tentative_claim: str) -> str:
    normalized = " ".join(tentative_claim.casefold().split())
    return sha256(f"pattern-question-v1:{account_id}:{normalized}".encode()).hexdigest()


def pattern_topic_key(claim: KnowledgeClaim) -> str:
    canonical = claim.model_dump_json()
    return sha256(f"pattern-topic-v1:{canonical}".encode()).hexdigest()


def pattern_evidence_hash(
    *,
    observation_started_at: datetime,
    observation_ended_at: datetime,
    supporting_examples: list[PatternEvidenceExample],
    counterexamples: list[PatternEvidenceExample],
) -> str:
    payload = {
        "observation_started_at": observation_started_at.isoformat(),
        "observation_ended_at": observation_ended_at.isoformat(),
        "supporting_examples": [
            item.model_dump(mode="json")
            for item in sorted(
                supporting_examples,
                key=lambda item: (item.evidence.kind, item.evidence.id),
            )
        ],
        "counterexamples": [
            item.model_dump(mode="json")
            for item in sorted(
                counterexamples,
                key=lambda item: (item.evidence.kind, item.evidence.id),
            )
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(f"pattern-evidence-v1:{canonical}".encode()).hexdigest()


def rich_pattern_question_id(
    *,
    account_id: str,
    topic_key: str,
    evidence_hash: str,
) -> str:
    return sha256(
        f"pattern-question-v2:{account_id}:{topic_key}:{evidence_hash}".encode()
    ).hexdigest()


def user_context_note_request_hash(request: UserContextNoteCreate) -> str:
    return sha256(request.model_dump_json().encode()).hexdigest()


def user_context_note_id(account_id: str, idempotency_key: str) -> str:
    return sha256(f"user-context-note-v1:{account_id}:{idempotency_key}".encode()).hexdigest()


def validate_purchase_list_limit(limit: int) -> None:
    if not 1 <= limit <= 50:
        raise ValueError("purchase list limit must be between 1 and 50")


def purchase_evidence_as_of(
    bundle: PurchaseEvidenceBundle,
    *,
    as_of: datetime,
) -> PurchaseEvidenceBundle | None:
    """Project immutable purchase revisions to the evidence available at one instant."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("purchase evidence cutoff requires a UTC offset")
    documents = [document for document in bundle.documents if document.created_at <= as_of]
    if not documents:
        return None
    documents.sort(key=lambda document: document.revision_number)
    document_ids = {document.id for document in documents}
    normalizations = [
        item for item in bundle.normalizations if item.document_id in document_ids
    ]
    normalized_document_ids = {item.document_id for item in normalizations}
    items = [item for item in bundle.items if item.document_id in document_ids]
    charges = [item for item in bundle.charges if item.document_id in document_ids]
    latest_confirmation = next(
        (
            document
            for document in reversed(documents)
            if document.kind == PurchaseDocumentKind.ORDER_CONFIRMATION
        ),
        None,
    )
    latest_final = next(
        (
            document
            for document in reversed(documents)
            if document.kind == PurchaseDocumentKind.FINAL_RECEIPT
        ),
        None,
    )
    projected_purchase = bundle.purchase.model_copy(
        update={
            "revision_count": len(documents),
            "latest_confirmation_document_id": (
                latest_confirmation.id if latest_confirmation is not None else None
            ),
            "latest_final_document_id": latest_final.id if latest_final is not None else None,
            "updated_at": max(document.created_at for document in documents),
        }
    )
    confirmation_id = (
        latest_confirmation.id
        if latest_confirmation is not None
        and latest_confirmation.id in normalized_document_ids
        else None
    )
    final_id = (
        latest_final.id
        if latest_final is not None and latest_final.id in normalized_document_ids
        else None
    )
    reconciliation = None
    if items and final_id is not None:
        reconciliation = reconcile_purchase_items(
            account_id=projected_purchase.account_id,
            purchase_id=projected_purchase.id,
            confirmation_document_id=confirmation_id,
            confirmation_items=[item for item in items if item.document_id == confirmation_id],
            final_document_id=final_id,
            final_items=[item for item in items if item.document_id == final_id],
        ).model_copy(update={"updated_at": projected_purchase.updated_at})
    return PurchaseEvidenceBundle(
        purchase=projected_purchase,
        documents=documents,
        normalizations=normalizations,
        items=items,
        charges=charges,
        reconciliation=reconciliation,
    )


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
        evidence.kind == KnowledgeEvidenceKind.KNOWLEDGE_REVISION and evidence.id == previous.id
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
    merchant_scope = candidate.merchant
    if candidate.evidence_origin != PurchaseEvidenceOrigin.AUTHENTICATED_EMAIL:
        merchant_scope = f"{candidate.merchant}:{candidate.evidence_origin.value}"
    if candidate.order_reference is not None:
        aliases.append(
            (
                purchase_identity_alias_id(
                    merchant=merchant_scope,
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
                    merchant=merchant_scope,
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
        or existing.evidence_origin != candidate.evidence_origin
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
    latest_capture = max(captures, key=capture_activity_time)
    return MealEntry(
        id=meal_id,
        account_id=event.account_id,
        capture_id=captures[0].id,
        event_id=event.id,
        occurred_at=event.last_capture_at,
        occurred_utc_offset_minutes=latest_capture.captured_utc_offset_minutes,
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
        clarification_reason=(hypothesis.question.justification if hypothesis.question else None),
        status=MealStatus.PROVISIONAL,
        revision_number=revision_number,
        created_at=created_at,
    )


class Repository(Protocol):
    async def provision_account(
        self,
        owner_user_id: str,
        *,
        verified_email_normalized: str | None = None,
    ) -> Account: ...

    async def change_public_account_capacity(
        self,
        *,
        account_id: str,
        action: AccountCapacityAction,
        reason: AccountCapacityReason,
        operation_id: str,
    ) -> AccountCapacityOperation: ...

    async def account_is_active(self, account_id: str) -> bool: ...

    async def append_audit_event(self, event: AuditEvent) -> AuditEvent: ...

    async def list_audit_events_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 100,
    ) -> list[AuditEvent]: ...

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

    async def create_account_export(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        requested_at: datetime,
        cooldown: timedelta,
    ) -> tuple[AccountExport, bool]: ...

    async def account_export_for_owner(
        self,
        *,
        owner_user_id: str,
        export_id: str,
    ) -> AccountExport: ...

    async def claim_account_export(
        self,
        *,
        account_id: str,
        export_id: str,
        lease_id: str,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> AccountExport | None: ...

    async def release_account_export(
        self,
        *,
        account_id: str,
        export_id: str,
        lease_id: str,
        available_at: datetime,
        error_code: str,
    ) -> bool: ...

    async def complete_account_export(
        self,
        *,
        account_id: str,
        export_id: str,
        lease_id: str,
        archive_object_key: str,
        archive_size: int,
        archive_sha256: str,
        manifest_sha256: str,
        completed_at: datetime,
        expires_at: datetime,
    ) -> AccountExport | None: ...

    async def fail_account_export(
        self,
        *,
        account_id: str,
        export_id: str,
        lease_id: str,
        error_code: str,
        failed_at: datetime,
    ) -> bool: ...

    async def create_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        address: str,
        address_hash: str,
    ) -> InboundMailAddress: ...

    async def rotate_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        expected_generation: int,
        address: str,
        address_hash: str,
    ) -> InboundMailAddress: ...

    async def revoke_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        expected_generation: int,
    ) -> InboundMailAddress: ...

    async def raw_mail_authentication(
        self,
        *,
        account_id: str,
        raw_mail_id: str,
    ) -> RawMailAuthentication | None: ...

    async def record_raw_mail_authentication(
        self,
        authentication: RawMailAuthentication,
    ) -> RawMailAuthentication: ...

    async def raw_mail_processing_disposition(
        self,
        *,
        account_id: str,
        raw_mail_id: str,
    ) -> RawMailProcessingDisposition | None: ...

    async def record_raw_mail_processing_disposition(
        self,
        disposition: RawMailProcessingDisposition,
    ) -> RawMailProcessingDisposition: ...

    async def attach_purchase_document(
        self,
        candidate: PurchaseDocumentCandidate,
    ) -> PurchaseIdentityResult: ...

    async def attach_synthetic_purchase_document(
        self,
        candidate: PurchaseDocumentCandidate,
        *,
        recorded_at: datetime,
    ) -> PurchaseIdentityResult: ...

    async def normalize_purchase_document(
        self,
        *,
        document: PurchaseDocument,
        parsed: ParsedPurchaseDocument,
    ) -> PurchaseNormalizationResult: ...

    async def list_purchases(
        self,
        owner_user_id: str,
        *,
        limit: int = 20,
    ) -> list[Purchase]: ...

    async def purchase_evidence_for_owner(
        self,
        owner_user_id: str,
        purchase_id: str,
    ) -> PurchaseEvidenceBundle: ...

    async def recent_purchase_evidence_for_account(
        self,
        *,
        account_id: str,
        as_of: datetime | None = None,
        limit: int = 5,
    ) -> list[PurchaseEvidenceBundle]: ...

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

    async def consent_preferences(
        self,
        *,
        firebase_uid: str,
    ) -> ConsentPreferences: ...

    async def withdraw_waitlist(
        self,
        *,
        firebase_uid: str,
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

    async def capture_for_account(
        self,
        *,
        account_id: str,
        capture_id: str,
    ) -> CaptureRecord: ...

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

    async def fail_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        error_code: str,
        error_message: str,
        failed_at: datetime,
    ) -> bool: ...

    async def settle_released_job_failure(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        expected_error_code: str,
        failed_at: datetime,
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

    async def record_ai_trace(self, trace: AiTraceRecord) -> AiTraceRecord: ...

    async def ai_trace_for_account(
        self,
        *,
        account_id: str,
        trace_id: str,
    ) -> AiTraceRecord: ...

    async def ai_traces_for_event(
        self,
        *,
        account_id: str,
        event_id: str,
        limit: int = 25,
    ) -> list[AiTraceRecord]: ...

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
        pattern_claim: KnowledgeClaim | None = None,
        observation_started_at: datetime | None = None,
        observation_ended_at: datetime | None = None,
        supporting_examples: list[PatternEvidenceExample] | None = None,
        counterexamples: list[PatternEvidenceExample] | None = None,
        prompt_version: str | None = None,
        uncertainty: str | None = None,
    ) -> ClarificationQuestion: ...

    async def list_meals(self, owner_user_id: str) -> list[MealEntry]: ...

    async def list_activity_history(
        self,
        owner_user_id: str,
        *,
        status: MealStatus | None = None,
    ) -> list[MealEntry]: ...

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

    async def knowledge_revision_result_for_request(
        self,
        *,
        account_id: str,
        idempotency_key: str,
    ) -> KnowledgeRevisionResult | None: ...

    async def current_knowledge_revision(
        self,
        *,
        account_id: str,
        topic_key: str,
    ) -> KnowledgeRevisionResult | None: ...

    async def knowledge_page_index_for_account(
        self,
        *,
        account_id: str,
        limit: int = 50,
    ) -> list[KnowledgePage]: ...

    async def active_knowledge_revision_for_account(
        self,
        *,
        account_id: str,
        page_id: str,
    ) -> KnowledgeRevisionResult: ...

    async def knowledge_page_for_owner(
        self,
        owner_user_id: str,
        page_id: str,
    ) -> KnowledgePage: ...

    async def list_knowledge_pages_for_owner(
        self,
        owner_user_id: str,
        *,
        include_retired: bool = False,
        limit: int = 50,
    ) -> list[KnowledgePage]: ...

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

    async def list_meal_feedback_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 200,
    ) -> list[MealFeedbackView]: ...

    async def list_questions(
        self,
        owner_user_id: str,
        *,
        question_status: QuestionStatus | None = None,
    ) -> list[ClarificationQuestion]: ...

    async def create_user_context_note(
        self,
        *,
        owner_user_id: str,
        request: UserContextNoteCreate,
        idempotency_key: str,
    ) -> UserContextNote: ...

    async def list_user_context_notes(
        self,
        owner_user_id: str,
        *,
        include_inactive: bool = False,
        active_at: datetime | None = None,
    ) -> list[UserContextNote]: ...

    async def retire_user_context_note(
        self,
        *,
        owner_user_id: str,
        note_id: str,
    ) -> UserContextNote: ...

    async def recent_meals_for_account(
        self,
        *,
        account_id: str,
        limit: int = 20,
    ) -> list[MealEntry]: ...

    async def recent_meal_evidence_for_account(
        self,
        *,
        account_id: str,
        limit: int = 20,
    ) -> list[tuple[MealEntry, MealRevision]]: ...

    async def active_user_context_notes_for_account(
        self,
        *,
        account_id: str,
        active_at: datetime | None = None,
        limit: int = 20,
    ) -> list[UserContextNote]: ...

    async def unresolved_reviews_for_account(
        self,
        *,
        account_id: str,
        limit: int = 20,
    ) -> tuple[list[MealEntry], list[ClarificationQuestion]]: ...

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

    async def list_question_responses_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 200,
    ) -> list[QuestionResponseView]: ...

    async def capture_for_owner(self, owner_user_id: str, capture_id: str) -> CaptureRecord: ...

    async def recent_captures_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 20,
    ) -> list[CaptureRecord]: ...


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
        self._ai_traces: dict[tuple[str, str], AiTraceRecord] = {}
        self._audit_events: dict[tuple[str, str], AuditEvent] = {}
        self._accounts: dict[str, Account] = {}
        self._account_by_owner: dict[str, str] = {}
        self._account_status_by_id: dict[str, str] = {}
        self._capacity_operations: dict[str, AccountCapacityOperation] = {}
        self._account_exports: dict[tuple[str, str], AccountExport] = {}
        self._active_export_by_account: dict[str, str] = {}
        self._last_export_requested_at: dict[str, datetime] = {}
        self._notification_outbox: dict[str, AccountCreatedOutbox] = {}
        self._launch_consents: dict[str, LaunchMailConsent] = {}
        self._launch_consent_state_by_owner: dict[str, LaunchMailConsent] = {}
        self._waitlist_by_identity_hash: dict[str, WaitlistEntry] = {}
        self._inbound_mail_addresses: dict[str, InboundMailAddress] = {}
        self._inbound_mail_routes: dict[str, InboundMailRoute] = {}
        self._published_raw_mail: dict[tuple[str, str], str] = {}
        self._raw_mail_authentication: dict[
            tuple[str, str], RawMailAuthentication
        ] = {}
        self._raw_mail_processing: dict[
            tuple[str, str], RawMailProcessingDisposition
        ] = {}
        self._purchases: dict[tuple[str, str], Purchase] = {}
        self._purchase_documents: dict[tuple[str, str], PurchaseDocument] = {}
        self._purchase_aliases: dict[tuple[str, str], PurchaseIdentityAlias] = {}
        self._purchase_normalizations: dict[tuple[str, str], PurchaseDocumentNormalization] = {}
        self._purchase_items: dict[tuple[str, str], PurchaseItem] = {}
        self._purchase_charges: dict[tuple[str, str], PurchaseCharge] = {}
        self._purchase_reconciliations: dict[tuple[str, str], PurchaseReconciliation] = {}
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
        self._latest_pattern_question_by_topic: dict[tuple[str, str], str] = {}
        self._question_responses: dict[str, QuestionResponse] = {}
        self._question_response_by_idempotency: dict[tuple[str, str], str] = {}
        self._user_context_notes: dict[str, UserContextNote] = {}
        self._user_context_note_request_hashes: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _require_active_account_locked(self, account_id: str) -> Account:
        account = self._accounts.get(account_id)
        if account is None or self._account_status_by_id.get(account_id) != "active":
            raise AccountNotProvisioned
        return account

    async def provision_account(
        self,
        owner_user_id: str,
        *,
        verified_email_normalized: str | None = None,
    ) -> Account:
        async with self._lock:
            existing_id = self._account_by_owner.get(owner_user_id)
            if existing_id:
                if self._account_status_by_id.get(existing_id) != "active":
                    raise AccountNotProvisioned
                return self._accounts[existing_id].model_copy(deep=True)
            entitlement_mode = (
                EntitlementMode.UNLIMITED
                if owner_user_id in self._unlimited_owner_user_ids
                else EntitlementMode.TRIAL
            )
            public_account_count = sum(
                account.entitlement_mode == EntitlementMode.TRIAL
                and self._account_status_by_id.get(account.id) == "active"
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
            waitlist_id = sha256(owner_user_id.encode()).hexdigest()
            waitlist = self._waitlist_by_identity_hash.get(waitlist_id)
            self._accounts[account.id] = account
            self._account_by_owner[owner_user_id] = account.id
            self._account_status_by_id[account.id] = "active"
            if waitlist is not None and waitlist.status == "active":
                self._waitlist_by_identity_hash[waitlist_id] = waitlist.model_copy(
                    update={
                        "status": "fulfilled",
                        "email_normalized": None,
                        "mailing_list_opt_in": False,
                        "fulfilled_at": account.created_at,
                        "fulfilled_account_id": account.id,
                        "updated_at": account.created_at,
                    }
                )
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

    async def change_public_account_capacity(
        self,
        *,
        account_id: str,
        action: AccountCapacityAction,
        reason: AccountCapacityReason,
        operation_id: str,
    ) -> AccountCapacityOperation:
        operation_key = "\0".join(("foodlog-capacity-v1", account_id, action.value, operation_id))
        event_id = sha256(operation_key.encode()).hexdigest()
        async with self._lock:
            existing = self._capacity_operations.get(event_id)
            if existing is not None:
                if existing.reason != reason:
                    raise AccountCapacityStateConflict
                return existing.model_copy(deep=True)
            account = self._accounts.get(account_id)
            if account is None or account.entitlement_mode != EntitlementMode.TRIAL:
                raise AccountCapacityStateConflict
            status = self._account_status_by_id.get(account_id)
            active_count = sum(
                candidate.entitlement_mode == EntitlementMode.TRIAL
                and self._account_status_by_id.get(candidate.id) == "active"
                for candidate in self._accounts.values()
            )
            if action == AccountCapacityAction.RECLAIM:
                if reason == AccountCapacityReason.OPERATOR_REVERSAL or status != "active":
                    raise AccountCapacityStateConflict
                if active_count < 1:
                    raise AccountCapacityStateConflict
                resulting_status = "capacity_reclaimed"
                resulting_count = active_count - 1
                audit_action = AuditAction.ACCOUNT_CAPACITY_RECLAIMED
            else:
                if reason != AccountCapacityReason.OPERATOR_REVERSAL:
                    raise AccountCapacityStateConflict
                if status != "capacity_reclaimed" or active_count >= self._public_account_limit:
                    raise AccountCapacityStateConflict
                resulting_status = "active"
                resulting_count = active_count + 1
                audit_action = AuditAction.ACCOUNT_CAPACITY_RESTORED
            operation = AccountCapacityOperation(
                id=event_id,
                operation_id=operation_id,
                account_id=account.id,
                owner_user_id=account.owner_user_id,
                action=action,
                reason=reason,
                previous_status=status,
                resulting_status=resulting_status,
                active_public_account_count=resulting_count,
                account_limit=self._public_account_limit,
            )
            self._account_status_by_id[account.id] = resulting_status
            self._capacity_operations[event_id] = operation
            self._append_audit_event_locked(
                build_audit_event(
                    account_id=account.id,
                    action=audit_action,
                    actor_kind=AuditActorKind.OPERATOR,
                    source=AuditSource.OPERATOR_CLI,
                    subject_kind="account_capacity",
                    subject_id=account.id,
                    purpose=AuditPurpose.SECURITY_REVIEW,
                    occurrence_id=operation_id,
                )
            )
            return operation.model_copy(deep=True)

    def _append_audit_event_locked(self, event: AuditEvent) -> AuditEvent:
        if event.account_id not in self._accounts:
            raise AccountNotProvisioned
        key = (event.account_id, event.id)
        existing = self._audit_events.get(key)
        if existing is not None:
            if existing.model_dump(exclude={"created_at"}) != event.model_dump(
                exclude={"created_at"}
            ):
                raise ValueError("audit event identity conflicts with existing evidence")
            return existing.model_copy(deep=True)
        self._audit_events[key] = event.model_copy(deep=True)
        return event.model_copy(deep=True)

    async def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        async with self._lock:
            return self._append_audit_event_locked(event)

    async def list_audit_events_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 100,
    ) -> list[AuditEvent]:
        if not 1 <= limit <= 200:
            raise ValueError("audit event list limit must be between 1 and 200")
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            events = sorted(
                (
                    event
                    for (account_id, _), event in self._audit_events.items()
                    if account_id == account.id
                ),
                key=lambda event: (event.created_at, event.id),
                reverse=True,
            )
            return [event.model_copy(deep=True) for event in events[:limit]]

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
            return self._require_active_account_locked(account_id).model_copy(deep=True)

    async def account_is_active(self, account_id: str) -> bool:
        async with self._lock:
            return self._account_status_by_id.get(account_id) == "active"

    async def create_account_export(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        requested_at: datetime,
        cooldown: timedelta,
    ) -> tuple[AccountExport, bool]:
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            raise ValueError("account export request time must include a UTC offset")
        if cooldown <= timedelta(0):
            raise ValueError("account export cooldown must be positive")
        async with self._lock:
            account_id = self._account_by_owner.get(owner_user_id)
            if not account_id:
                raise AccountNotProvisioned
            self._require_active_account_locked(account_id)
            export_id = account_export_id(account_id, idempotency_key)
            key = (account_id, export_id)
            existing = self._account_exports.get(key)
            if existing is not None:
                job = self._jobs.get((account_id, existing.job_id))
                if (
                    existing.requested_by_user_id != owner_user_id
                    or job is None
                    or job.kind != JobKind.ACCOUNT_EXPORT
                    or job.subject_id != export_id
                ):
                    raise JobIdentityConflict
                return existing.model_copy(deep=True), False

            active_export_id = self._active_export_by_account.get(account_id)
            if active_export_id is not None:
                raise AccountExportAlreadyActive(active_export_id)
            last_requested_at = self._last_export_requested_at.get(account_id)
            if last_requested_at is not None:
                available_at = last_requested_at + cooldown
                if requested_at < available_at:
                    raise AccountExportRateLimited(
                        max(1, ceil((available_at - requested_at).total_seconds()))
                    )

            job_id = account_export_job_id(export_id)
            account_export = AccountExport(
                id=export_id,
                account_id=account_id,
                requested_by_user_id=owner_user_id,
                job_id=job_id,
                snapshot_at=requested_at,
                requested_at=requested_at,
            )
            job = DurableJob(
                id=job_id,
                account_id=account_id,
                kind=JobKind.ACCOUNT_EXPORT,
                subject_id=export_id,
                subject_revision=1,
                available_at=requested_at,
                created_at=requested_at,
            )
            audit = build_audit_event(
                account_id=account_id,
                action=AuditAction.ACCOUNT_EXPORT_REQUESTED,
                actor_kind=AuditActorKind.USER,
                source=AuditSource.API,
                subject_kind="account_export",
                subject_id=export_id,
            ).model_copy(update={"created_at": requested_at})
            self._account_exports[key] = account_export
            self._jobs[(account_id, job_id)] = job
            self._active_export_by_account[account_id] = export_id
            self._last_export_requested_at[account_id] = requested_at
            self._append_audit_event_locked(audit)
            return account_export.model_copy(deep=True), True

    async def account_export_for_owner(
        self,
        *,
        owner_user_id: str,
        export_id: str,
    ) -> AccountExport:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            account_export = self._account_exports.get((account.id, export_id))
            if account_export is None or account_export.requested_by_user_id != owner_user_id:
                raise AccountExportNotFound
            return account_export.model_copy(deep=True)

    async def claim_account_export(
        self,
        *,
        account_id: str,
        export_id: str,
        lease_id: str,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> AccountExport | None:
        now = utc_now()
        if lease_expires_at <= now:
            raise ValueError("account export lease must expire in the future")
        async with self._lock:
            if self._account_status_by_id.get(account_id) != "active":
                return None
            account_export = self._account_exports.get((account_id, export_id))
            if account_export is None:
                raise AccountExportNotFound
            if account_export.status in {
                AccountExportStatus.COMPLETED,
                AccountExportStatus.FAILED,
            }:
                return None
            job = self._jobs.get((account_id, account_export.job_id))
            if job is None or job.kind != JobKind.ACCOUNT_EXPORT or job.subject_id != export_id:
                raise JobIdentityConflict
            if job.status == JobStatus.LEASED and (
                job.lease_expires_at is None or job.lease_expires_at > now
            ):
                return None
            if job.status not in {JobStatus.PENDING, JobStatus.LEASED}:
                return None
            if job.status == JobStatus.PENDING and job.available_at > now:
                return None
            leased_job = job.model_copy(
                update={
                    "status": JobStatus.LEASED,
                    "attempt_count": job.attempt_count + 1,
                    "lease_id": lease_id,
                    "lease_owner": lease_owner,
                    "lease_expires_at": lease_expires_at,
                    "last_error_code": None,
                    "last_error_message": None,
                }
            )
            building = account_export.model_copy(
                update={
                    "status": AccountExportStatus.BUILDING,
                    "last_error_code": None,
                }
            )
            self._jobs[(account_id, job.id)] = leased_job
            self._account_exports[(account_id, export_id)] = building
            return building.model_copy(deep=True)

    async def release_account_export(
        self,
        *,
        account_id: str,
        export_id: str,
        lease_id: str,
        available_at: datetime,
        error_code: str,
    ) -> bool:
        async with self._lock:
            if self._account_status_by_id.get(account_id) != "active":
                return False
            account_export = self._account_exports.get((account_id, export_id))
            if account_export is None:
                raise AccountExportNotFound
            job = self._jobs.get((account_id, account_export.job_id))
            if (
                account_export.status != AccountExportStatus.BUILDING
                or job is None
                or job.status != JobStatus.LEASED
                or job.lease_id != lease_id
            ):
                return False
            self._jobs[(account_id, job.id)] = job.model_copy(
                update={
                    "status": JobStatus.PENDING,
                    "available_at": available_at,
                    "lease_id": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error_code": error_code,
                    "last_error_message": None,
                }
            )
            self._account_exports[(account_id, export_id)] = account_export.model_copy(
                update={
                    "status": AccountExportStatus.PENDING,
                    "last_error_code": error_code,
                }
            )
            return True

    async def complete_account_export(
        self,
        *,
        account_id: str,
        export_id: str,
        lease_id: str,
        archive_object_key: str,
        archive_size: int,
        archive_sha256: str,
        manifest_sha256: str,
        completed_at: datetime,
        expires_at: datetime,
    ) -> AccountExport | None:
        async with self._lock:
            if self._account_status_by_id.get(account_id) != "active":
                return None
            account_export = self._account_exports.get((account_id, export_id))
            if account_export is None:
                raise AccountExportNotFound
            if account_export.status == AccountExportStatus.COMPLETED:
                return account_export.model_copy(deep=True)
            job = self._jobs.get((account_id, account_export.job_id))
            if (
                account_export.status != AccountExportStatus.BUILDING
                or job is None
                or job.status != JobStatus.LEASED
                or job.lease_id != lease_id
            ):
                return None
            completed = account_export.model_copy(
                update={
                    "status": AccountExportStatus.COMPLETED,
                    "archive_object_key": archive_object_key,
                    "archive_size": archive_size,
                    "archive_sha256": archive_sha256,
                    "manifest_sha256": manifest_sha256,
                    "completed_at": completed_at,
                    "expires_at": expires_at,
                    "last_error_code": None,
                }
            )
            completed_job = job.model_copy(
                update={
                    "status": JobStatus.COMPLETED,
                    "lease_id": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "completed_at": completed_at,
                    "last_error_code": None,
                    "last_error_message": None,
                }
            )
            self._account_exports[(account_id, export_id)] = completed
            self._jobs[(account_id, job.id)] = completed_job
            if self._active_export_by_account.get(account_id) == export_id:
                self._active_export_by_account.pop(account_id)
            return completed.model_copy(deep=True)

    async def fail_account_export(
        self,
        *,
        account_id: str,
        export_id: str,
        lease_id: str,
        error_code: str,
        failed_at: datetime,
    ) -> bool:
        async with self._lock:
            if self._account_status_by_id.get(account_id) != "active":
                return False
            account_export = self._account_exports.get((account_id, export_id))
            if account_export is None:
                raise AccountExportNotFound
            job = self._jobs.get((account_id, account_export.job_id))
            if (
                account_export.status != AccountExportStatus.BUILDING
                or job is None
                or job.status != JobStatus.LEASED
                or job.lease_id != lease_id
            ):
                return False
            self._account_exports[(account_id, export_id)] = account_export.model_copy(
                update={
                    "status": AccountExportStatus.FAILED,
                    "failed_at": failed_at,
                    "last_error_code": error_code,
                }
            )
            self._jobs[(account_id, job.id)] = job.model_copy(
                update={
                    "status": JobStatus.FAILED,
                    "lease_id": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "failed_at": failed_at,
                    "last_error_code": error_code,
                    "last_error_message": None,
                }
            )
            if self._active_export_by_account.get(account_id) == export_id:
                self._active_export_by_account.pop(account_id)
            return True

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
            self._require_active_account_locked(account_id)
            existing = self._inbound_mail_addresses.get(account_id)
            if existing is not None:
                route = self._inbound_mail_routes.get(
                    sha256(existing.address.casefold().encode()).hexdigest()
                )
                if (
                    route is None
                    or route.account_id != account_id
                    or route.status != existing.status
                    or route.generation != existing.generation
                ):
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

    async def rotate_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        expected_generation: int,
        address: str,
        address_hash: str,
    ) -> InboundMailAddress:
        async with self._lock:
            account_id = self._account_by_owner.get(owner_user_id)
            if not account_id:
                raise AccountNotProvisioned
            self._require_active_account_locked(account_id)
            current = self._inbound_mail_addresses.get(account_id)
            if current is None or current.generation != expected_generation:
                raise InboundAddressStateConflict
            current_hash = sha256(current.address.casefold().encode()).hexdigest()
            current_route = self._inbound_mail_routes.get(current_hash)
            if (
                current_route is None
                or current_route.account_id != account_id
                or current_route.status != current.status
                or current_route.generation != current.generation
            ):
                raise InboundAddressStateConflict
            if address_hash in self._inbound_mail_routes:
                raise InboundAddressCollision
            now = utc_now()
            revoked_route = current_route
            if current_route.status == InboundMailAddressStatus.ACTIVE:
                revoked_route = current_route.model_copy(
                    update={
                        "status": InboundMailAddressStatus.REVOKED,
                        "revoked_at": now,
                    }
                )
            replacement = InboundMailAddress(
                account_id=account_id,
                address=address,
                generation=current.generation + 1,
                created_at=now,
            )
            replacement_route = InboundMailRoute(
                id=address_hash,
                account_id=account_id,
                generation=replacement.generation,
                created_at=now,
            )
            self._inbound_mail_routes[current_hash] = revoked_route
            self._inbound_mail_routes[address_hash] = replacement_route
            self._inbound_mail_addresses[account_id] = replacement
            return replacement.model_copy(deep=True)

    async def revoke_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        expected_generation: int,
    ) -> InboundMailAddress:
        async with self._lock:
            account_id = self._account_by_owner.get(owner_user_id)
            if not account_id:
                raise AccountNotProvisioned
            self._require_active_account_locked(account_id)
            current = self._inbound_mail_addresses.get(account_id)
            if current is None or current.generation != expected_generation:
                raise InboundAddressStateConflict
            current_hash = sha256(current.address.casefold().encode()).hexdigest()
            route = self._inbound_mail_routes.get(current_hash)
            if (
                route is None
                or route.account_id != account_id
                or route.status != current.status
                or route.generation != current.generation
            ):
                raise InboundAddressStateConflict
            if current.status == InboundMailAddressStatus.REVOKED:
                return current.model_copy(deep=True)
            now = utc_now()
            revoked = current.model_copy(
                update={
                    "status": InboundMailAddressStatus.REVOKED,
                    "revoked_at": now,
                }
            )
            self._inbound_mail_addresses[account_id] = revoked
            self._inbound_mail_routes[current_hash] = route.model_copy(
                update={
                    "status": InboundMailAddressStatus.REVOKED,
                    "revoked_at": now,
                }
            )
            return revoked.model_copy(deep=True)

    async def seed_published_raw_mail(
        self,
        *,
        account_id: str,
        raw_mail_id: str,
        content_sha256: str,
    ) -> None:
        """Seed transport evidence in the in-memory adapter used by local workers/tests."""

        async with self._lock:
            self._require_active_account_locked(account_id)
            self._published_raw_mail[(account_id, raw_mail_id)] = content_sha256

    async def raw_mail_authentication(
        self,
        *,
        account_id: str,
        raw_mail_id: str,
    ) -> RawMailAuthentication | None:
        async with self._lock:
            authentication = self._raw_mail_authentication.get((account_id, raw_mail_id))
            return authentication.model_copy(deep=True) if authentication else None

    async def record_raw_mail_authentication(
        self,
        authentication: RawMailAuthentication,
    ) -> RawMailAuthentication:
        async with self._lock:
            if self._account_status_by_id.get(authentication.account_id) != "active":
                raise AccountNotProvisioned
            raw_content_sha256 = self._published_raw_mail.get(
                (authentication.account_id, authentication.raw_mail_id)
            )
            if raw_content_sha256 != authentication.raw_content_sha256:
                raise RawMailNotFound
            key = (authentication.account_id, authentication.raw_mail_id)
            existing = self._raw_mail_authentication.get(key)
            if existing is not None:
                if existing.model_dump(exclude={"verified_at"}) != authentication.model_dump(
                    exclude={"verified_at"}
                ):
                    raise RawMailAuthenticationConflict
                return existing.model_copy(deep=True)
            self._raw_mail_authentication[key] = authentication.model_copy(deep=True)
            return authentication.model_copy(deep=True)

    async def raw_mail_processing_disposition(
        self,
        *,
        account_id: str,
        raw_mail_id: str,
    ) -> RawMailProcessingDisposition | None:
        async with self._lock:
            disposition = self._raw_mail_processing.get((account_id, raw_mail_id))
            return disposition.model_copy(deep=True) if disposition else None

    async def record_raw_mail_processing_disposition(
        self,
        disposition: RawMailProcessingDisposition,
    ) -> RawMailProcessingDisposition:
        async with self._lock:
            if self._account_status_by_id.get(disposition.account_id) != "active":
                raise AccountNotProvisioned
            key = (disposition.account_id, disposition.raw_mail_id)
            if self._published_raw_mail.get(key) != disposition.raw_content_sha256:
                raise RawMailNotFound
            authentication = self._raw_mail_authentication.get(key)
            if (
                authentication is None
                or authentication.raw_content_sha256 != disposition.raw_content_sha256
                or authentication.outcome
                != RawMailAuthenticationOutcome.ALIGNED_DKIM_PASS
            ):
                raise RawMailNotFound
            existing = self._raw_mail_processing.get(key)
            if existing is not None:
                if existing.model_dump(exclude={"created_at"}) != disposition.model_dump(
                    exclude={"created_at"}
                ):
                    raise RawMailProcessingConflict
                return existing.model_copy(deep=True)
            self._raw_mail_processing[key] = disposition.model_copy(deep=True)
            return disposition.model_copy(deep=True)

    async def attach_purchase_document(
        self,
        candidate: PurchaseDocumentCandidate,
    ) -> PurchaseIdentityResult:
        if candidate.evidence_origin != PurchaseEvidenceOrigin.AUTHENTICATED_EMAIL:
            raise PurchaseIdentityConflict
        return await self._attach_purchase_document(
            candidate,
            require_authenticated_mail=True,
            recorded_at=None,
        )

    async def attach_synthetic_purchase_document(
        self,
        candidate: PurchaseDocumentCandidate,
        *,
        recorded_at: datetime,
    ) -> PurchaseIdentityResult:
        if candidate.evidence_origin != PurchaseEvidenceOrigin.SYNTHETIC_EVALUATION:
            raise PurchaseIdentityConflict
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("synthetic purchase timestamps require a UTC offset")
        return await self._attach_purchase_document(
            candidate,
            require_authenticated_mail=False,
            recorded_at=recorded_at,
        )

    async def _attach_purchase_document(
        self,
        candidate: PurchaseDocumentCandidate,
        *,
        require_authenticated_mail: bool,
        recorded_at: datetime | None,
    ) -> PurchaseIdentityResult:
        async with self._lock:
            if self._account_status_by_id.get(candidate.account_id) != "active":
                raise AccountNotProvisioned
            if require_authenticated_mail:
                raw_content_sha256 = self._published_raw_mail.get(
                    (candidate.account_id, candidate.raw_mail_id)
                )
                if raw_content_sha256 != candidate.raw_content_sha256:
                    raise RawMailNotFound
                authentication = self._raw_mail_authentication.get(
                    (candidate.account_id, candidate.raw_mail_id)
                )
                if (
                    authentication is None
                    or authentication.raw_content_sha256 != candidate.raw_content_sha256
                    or authentication.outcome != RawMailAuthenticationOutcome.ALIGNED_DKIM_PASS
                ):
                    raise RawMailNotFound

            document_key = (candidate.account_id, candidate.raw_mail_id)
            existing_document = self._purchase_documents.get(document_key)
            if existing_document is not None:
                validate_purchase_document_retry(existing_document, candidate)
                if recorded_at is not None and existing_document.created_at != recorded_at:
                    raise PurchaseDocumentConflict
                purchase = self._purchases.get(
                    (candidate.account_id, existing_document.purchase_id)
                )
                if (
                    purchase is None
                    or purchase.merchant != candidate.merchant
                    or purchase.evidence_origin != candidate.evidence_origin
                ):
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

            now = recorded_at or utc_now()
            if purchase_ids:
                purchase_id = purchase_ids.pop()
                purchase = self._purchases.get((candidate.account_id, purchase_id))
                if (
                    purchase is None
                    or purchase.merchant != candidate.merchant
                    or purchase.evidence_origin != candidate.evidence_origin
                ):
                    raise PurchaseIdentityConflict
            else:
                purchase = Purchase(
                    id=str(uuid4()),
                    account_id=candidate.account_id,
                    merchant=candidate.merchant,
                    evidence_origin=candidate.evidence_origin,
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
                evidence_origin=candidate.evidence_origin,
                kind=candidate.kind,
                revision_number=revision_number,
                order_reference=candidate.order_reference,
                invoice_reference=candidate.invoice_reference,
                created_at=now,
            )
            latest_update = {}
            if candidate.kind == PurchaseDocumentKind.ORDER_CONFIRMATION:
                latest_update["latest_confirmation_document_id"] = document.id
            elif candidate.kind == PurchaseDocumentKind.FINAL_RECEIPT:
                latest_update["latest_final_document_id"] = document.id
            purchase = purchase.model_copy(
                update={
                    "revision_count": revision_number,
                    "updated_at": now,
                    **latest_update,
                }
            )
            self._purchases[(candidate.account_id, purchase.id)] = purchase
            self._purchase_documents[document_key] = document
            return PurchaseIdentityResult(
                purchase=purchase.model_copy(deep=True),
                document=document.model_copy(deep=True),
                duplicate=False,
            )

    async def normalize_purchase_document(
        self,
        *,
        document: PurchaseDocument,
        parsed: ParsedPurchaseDocument,
    ) -> PurchaseNormalizationResult:
        normalization, items, charges = materialize_purchase_document_normalization(
            document=document,
            parsed=parsed,
        )
        async with self._lock:
            if self._account_status_by_id.get(document.account_id) != "active":
                raise AccountNotProvisioned
            source = self._purchase_documents.get((document.account_id, document.id))
            purchase = self._purchases.get((document.account_id, document.purchase_id))
            if source != document or purchase is None:
                raise PurchaseNormalizationConflict
            existing = self._purchase_normalizations.get((document.account_id, document.id))
            if existing is not None:
                if existing.model_dump(exclude={"created_at"}) != normalization.model_dump(
                    exclude={"created_at"}
                ):
                    raise PurchaseNormalizationConflict
                persisted_items = [
                    self._purchase_items[(document.account_id, item.id)] for item in items
                ]
                persisted_charges = [
                    self._purchase_charges[(document.account_id, charge.id)] for charge in charges
                ]
                reconciliation = self._purchase_reconciliations.get(
                    (document.account_id, document.purchase_id)
                )
                if reconciliation is None:
                    raise PurchaseNormalizationConflict
                return PurchaseNormalizationResult(
                    normalization=existing.model_copy(deep=True),
                    items=[item.model_copy(deep=True) for item in persisted_items],
                    charges=[charge.model_copy(deep=True) for charge in persisted_charges],
                    reconciliation=reconciliation.model_copy(deep=True),
                    duplicate=True,
                )

            self._purchase_normalizations[(document.account_id, document.id)] = normalization
            for item in items:
                self._purchase_items[(document.account_id, item.id)] = item
            for charge in charges:
                self._purchase_charges[(document.account_id, charge.id)] = charge

            confirmation_id = purchase.latest_confirmation_document_id
            final_id = purchase.latest_final_document_id
            confirmation_items = [
                item
                for (account_id, _), item in self._purchase_items.items()
                if account_id == document.account_id and item.document_id == confirmation_id
            ]
            final_items = [
                item
                for (account_id, _), item in self._purchase_items.items()
                if account_id == document.account_id and item.document_id == final_id
            ]
            reconciliation = reconcile_purchase_items(
                account_id=document.account_id,
                purchase_id=document.purchase_id,
                confirmation_document_id=(confirmation_id if confirmation_items else None),
                confirmation_items=confirmation_items,
                final_document_id=final_id if final_items else None,
                final_items=final_items,
            )
            self._purchase_reconciliations[(document.account_id, document.purchase_id)] = (
                reconciliation
            )
            return PurchaseNormalizationResult(
                normalization=normalization.model_copy(deep=True),
                items=[item.model_copy(deep=True) for item in items],
                charges=[charge.model_copy(deep=True) for charge in charges],
                reconciliation=reconciliation.model_copy(deep=True),
                duplicate=False,
            )

    async def list_purchases(
        self,
        owner_user_id: str,
        *,
        limit: int = 20,
    ) -> list[Purchase]:
        validate_purchase_list_limit(limit)
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            purchases = sorted(
                (
                    purchase
                    for (account_id, _), purchase in self._purchases.items()
                    if account_id == account.id
                ),
                key=lambda purchase: purchase.updated_at,
                reverse=True,
            )
            return [purchase.model_copy(deep=True) for purchase in purchases[:limit]]

    async def purchase_evidence_for_owner(
        self,
        owner_user_id: str,
        purchase_id: str,
    ) -> PurchaseEvidenceBundle:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            return self._purchase_evidence_unlocked(account.id, purchase_id).model_copy(deep=True)

    async def recent_purchase_evidence_for_account(
        self,
        *,
        account_id: str,
        as_of: datetime | None = None,
        limit: int = 5,
    ) -> list[PurchaseEvidenceBundle]:
        validate_purchase_list_limit(limit)
        if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
            raise ValueError("purchase evidence cutoff requires a UTC offset")
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            bundles = [
                self._purchase_evidence_unlocked(account_id, purchase.id)
                for (scope, _), purchase in self._purchases.items()
                if scope == account_id
            ]
            if as_of is not None:
                bundles = [
                    projected
                    for bundle in bundles
                    if (projected := purchase_evidence_as_of(bundle, as_of=as_of)) is not None
                ]
            bundles.sort(key=lambda bundle: bundle.purchase.updated_at, reverse=True)
            return [bundle.model_copy(deep=True) for bundle in bundles[:limit]]

    def _purchase_evidence_unlocked(
        self,
        account_id: str,
        purchase_id: str,
    ) -> PurchaseEvidenceBundle:
        purchase = self._purchases.get((account_id, purchase_id))
        if purchase is None:
            raise PurchaseNotFound
        documents = sorted(
            (
                document
                for (scope, _), document in self._purchase_documents.items()
                if scope == account_id and document.purchase_id == purchase_id
            ),
            key=lambda document: document.revision_number,
        )
        document_ids = {document.id for document in documents}
        normalizations = sorted(
            (
                normalization
                for (scope, _), normalization in self._purchase_normalizations.items()
                if scope == account_id
                and normalization.purchase_id == purchase_id
                and normalization.document_id in document_ids
            ),
            key=lambda normalization: normalization.document_revision_number,
        )
        items = sorted(
            (
                item
                for (scope, _), item in self._purchase_items.items()
                if scope == account_id and item.purchase_id == purchase_id
            ),
            key=lambda item: (item.document_revision_number, item.ordinal),
        )
        document_revisions = {document.id: document.revision_number for document in documents}
        charges = sorted(
            (
                charge
                for (scope, _), charge in self._purchase_charges.items()
                if scope == account_id and charge.purchase_id == purchase_id
            ),
            key=lambda charge: (
                document_revisions.get(charge.document_id, 0),
                charge.kind.value,
            ),
        )
        reconciliation = self._purchase_reconciliations.get((account_id, purchase_id))
        return PurchaseEvidenceBundle(
            purchase=purchase,
            documents=documents,
            normalizations=normalizations,
            items=items,
            charges=charges,
            reconciliation=reconciliation,
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
            self._require_active_account_locked(account.id)
            existing = self._launch_consents.get(consent_id)
            if existing:
                self._launch_consent_state_by_owner[owner_user_id] = existing
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
            self._launch_consent_state_by_owner[owner_user_id] = consent
            return consent.model_copy(deep=True)

    async def join_waitlist(
        self,
        *,
        firebase_uid: str,
        email_normalized: str,
        policy_version: str,
    ) -> WaitlistEntry:
        waitlist_id = sha256(firebase_uid.encode()).hexdigest()
        async with self._lock:
            if firebase_uid in self._account_by_owner:
                raise AccountAlreadyProvisioned
            existing = self._waitlist_by_identity_hash.get(waitlist_id)
            if existing and existing.status == "fulfilled":
                raise AccountAlreadyProvisioned
            if existing and existing.status == "active":
                return existing.model_copy(deep=True)
            public_account_count = sum(
                account.entitlement_mode == EntitlementMode.TRIAL
                and self._account_status_by_id.get(account.id) == "active"
                for account in self._accounts.values()
            )
            if public_account_count < self._public_account_limit:
                raise WaitlistUnavailable
            now = utc_now()
            entry = (
                existing.model_copy(
                    update={
                        "email_normalized": email_normalized,
                        "policy_version": policy_version,
                        "mailing_list_opt_in": True,
                        "status": "active",
                        "updated_at": now,
                    },
                )
                if existing
                else WaitlistEntry(
                    id=waitlist_id,
                    email_normalized=email_normalized,
                    policy_version=policy_version,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._waitlist_by_identity_hash[waitlist_id] = entry
            return entry.model_copy(deep=True)

    async def consent_preferences(
        self,
        *,
        firebase_uid: str,
    ) -> ConsentPreferences:
        async with self._lock:
            account_id = self._account_by_owner.get(firebase_uid)
            if account_id is not None:
                self._require_active_account_locked(account_id)
            launch = self._launch_consent_state_by_owner.get(firebase_uid)
            waitlist = self._waitlist_by_identity_hash.get(
                sha256(firebase_uid.encode()).hexdigest()
            )
            return ConsentPreferences(
                launch_mail_opt_in=launch.granted if launch else None,
                launch_mail_policy_version=launch.policy_version if launch else None,
                launch_mail_updated_at=launch.created_at if launch else None,
                waitlist_status=waitlist.status if waitlist else "not_joined",
                waitlist_policy_version=waitlist.policy_version if waitlist else None,
                waitlist_updated_at=waitlist.updated_at if waitlist else None,
            )

    async def withdraw_waitlist(
        self,
        *,
        firebase_uid: str,
    ) -> WaitlistEntry:
        waitlist_id = sha256(firebase_uid.encode()).hexdigest()
        async with self._lock:
            existing = self._waitlist_by_identity_hash.get(waitlist_id)
            if existing is None:
                raise WaitlistEntryNotFound
            if existing.status == "withdrawn":
                return existing.model_copy(deep=True)
            if existing.status == "fulfilled":
                raise AccountAlreadyProvisioned
            withdrawn_at = utc_now()
            withdrawn = existing.model_copy(
                update={
                    "email_normalized": None,
                    "mailing_list_opt_in": False,
                    "status": "withdrawn",
                    "updated_at": withdrawn_at,
                    "last_withdrawn_at": withdrawn_at,
                    "withdrawal_count": existing.withdrawal_count + 1,
                },
            )
            self._waitlist_by_identity_hash[waitlist_id] = withdrawn
            return withdrawn.model_copy(deep=True)

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
            self._require_active_account_locked(account.id)
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
            try:
                account = self._require_active_account_locked(credential.account_id)
            except AccountNotProvisioned:
                raise InvalidDeviceCredential from None
            if (
                camera is None
                or camera.account_id != credential.account_id
                or camera.status != CameraStatus.ACTIVE
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
            self._require_active_account_locked(account.id)
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
            try:
                self._require_active_account_locked(account_id)
            except AccountNotProvisioned:
                raise CameraNotFound from None
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
            self._require_active_account_locked(account.id)
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
            if (
                stored_account is None
                or stored_account.owner_user_id != account.owner_user_id
                or self._account_status_by_id.get(account.id) != "active"
            ):
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
                captured_utc_offset_minutes=(
                    int(metadata.captured_at.utcoffset().total_seconds() // 60)
                    if metadata is not None
                    else None
                ),
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
            if self._account_status_by_id.get(account_id) != "active":
                raise AccountNotProvisioned
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
            if self._account_status_by_id.get(account_id) != "active":
                raise AccountNotProvisioned
            capture = self._captures.get(capture_id)
            if not capture or capture.account_id != account_id:
                raise CaptureNotFound
            if capture.status == CaptureStatus.ACCEPTED:
                camera = self._cameras.get(capture.camera_id) or self._device_cameras.get(
                    capture.camera_id
                )
                if camera is None or camera.account_id != account_id:
                    raise CameraNotFound
                capture.status = CaptureStatus.STORED
                camera.accepted_capture_count += 1
                camera.last_capture_at = max(
                    filter(None, (camera.last_capture_at, capture.created_at))
                )
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
            self._require_active_account_locked(account_id)
            capture = self._captures.get(capture_id)
            if not capture or capture.account_id != account_id:
                raise CaptureNotFound
            capture.status = CaptureStatus.PROCESSED

    async def capture_for_account(
        self,
        *,
        account_id: str,
        capture_id: str,
    ) -> CaptureRecord:
        async with self._lock:
            capture = self._captures.get(capture_id)
            if capture is None or capture.account_id != account_id:
                raise CaptureNotFound
            return capture.model_copy(deep=True)

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
        validate_enqueueable_job(job)
        async with self._lock:
            self._require_active_account_locked(job.account_id)
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
            if self._account_status_by_id.get(account_id) != "active":
                return None
            now = utc_now()
            if lease_expires_at <= now:
                raise ValueError("Job leases must expire in the future")
            key = (account_id, job_id)
            job = self._jobs.get(key)
            if job is None or job.subject_revision != expected_subject_revision:
                return None
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
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
            try:
                self._require_active_account_locked(account_id)
            except AccountNotProvisioned:
                return False
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
            try:
                self._require_active_account_locked(account_id)
            except AccountNotProvisioned:
                return False
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

    async def fail_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        error_code: str,
        error_message: str,
        failed_at: datetime,
    ) -> bool:
        async with self._lock:
            try:
                self._require_active_account_locked(account_id)
            except AccountNotProvisioned:
                return False
            key = (account_id, job_id)
            job = self._jobs.get(key)
            if not self._job_has_active_lease(
                job,
                expected_subject_revision=expected_subject_revision,
                lease_id=lease_id,
                lease_owner=lease_owner,
                now=failed_at,
            ):
                return False
            assert job is not None
            self._jobs[key] = self._updated_job(
                job,
                status=JobStatus.FAILED,
                lease_id=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=error_code,
                last_error_message=error_message,
                completed_at=None,
                failed_at=failed_at,
            )
            return True

    async def settle_released_job_failure(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        expected_error_code: str,
        failed_at: datetime,
    ) -> bool:
        async with self._lock:
            if self._account_status_by_id.get(account_id) != "active":
                return False
            key = (account_id, job_id)
            job = self._jobs.get(key)
            if (
                job is None
                or job.status != JobStatus.PENDING
                or job.subject_revision != expected_subject_revision
                or job.last_error_code != expected_error_code
            ):
                return False
            self._jobs[key] = self._updated_job(
                job,
                status=JobStatus.FAILED,
                completed_at=None,
                failed_at=failed_at,
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
            if self._account_status_by_id.get(account_id) != "active":
                return None
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
            if self._account_status_by_id.get(account_id) != "active":
                return None
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
            revision_number = existing_meal.revision_number + 1 if existing_meal is not None else 1
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
            if self._account_status_by_id.get(reservation.account_id) != "active":
                raise AccountNotProvisioned
            existing = self._model_spend_reservations.get(reservation.id)
            if existing is not None:
                if existing.model_dump(exclude={"created_at"}) != reservation.model_dump(
                    exclude={"created_at"}
                ):
                    raise ModelSpendReservationConflict
                return existing.model_copy(deep=True)
            proposed_total = self._model_spend_reserved_dkk_micros + reservation.reserved_dkk_micros
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
            self._model_spend_reserved_dkk_micros = (
                self._model_spend_reserved_dkk_micros
                - reservation.reserved_dkk_micros
                + stored.actual_dkk_micros
            )
            self._model_spend_actual_dkk_micros += stored.actual_dkk_micros
            return stored.model_copy(deep=True)

    async def record_ai_trace(self, trace: AiTraceRecord) -> AiTraceRecord:
        async with self._lock:
            account_status = self._account_status_by_id.get(trace.account_id)
            if account_status not in {
                "active",
                "capacity_reclaimed",
            }:
                raise AccountNotProvisioned
            usage = self._model_usage.get(trace.reservation_id)
            if account_status == "capacity_reclaimed" and usage is None:
                raise AiTraceConflict
            if usage is not None:
                validate_ai_trace_usage(trace, usage)
            key = (trace.account_id, trace.id)
            existing = self._ai_traces.get(key)
            if existing is not None:
                if existing != trace:
                    raise AiTraceConflict
                stored = existing.model_copy(deep=True)
            else:
                self._ai_traces[key] = trace.model_copy(deep=True)
                stored = trace.model_copy(deep=True)
            self._append_audit_event_locked(
                build_audit_event(
                    account_id=trace.account_id,
                    action=AuditAction.AI_TRACE_RECORDED,
                    actor_kind=AuditActorKind.SYSTEM,
                    source=AuditSource.AGENT,
                    subject_kind="trace",
                    subject_id=trace.id,
                )
            )
            return stored

    async def ai_trace_for_account(
        self,
        *,
        account_id: str,
        trace_id: str,
    ) -> AiTraceRecord:
        async with self._lock:
            trace = self._ai_traces.get((account_id, trace_id))
            if trace is None:
                raise AiTraceNotFound
            return trace.model_copy(deep=True)

    async def ai_traces_for_event(
        self,
        *,
        account_id: str,
        event_id: str,
        limit: int = 25,
    ) -> list[AiTraceRecord]:
        if not 1 <= limit <= 25:
            raise ValueError("AI trace event limit must be between 1 and 25")
        async with self._lock:
            if (account_id, event_id) not in self._events:
                raise ActivityEventNotFound
            traces = sorted(
                (
                    trace
                    for (trace_account_id, _), trace in self._ai_traces.items()
                    if trace_account_id == account_id and trace.event_id == event_id
                ),
                key=lambda trace: (trace.created_at, trace.id),
            )
            if len(traces) > limit:
                raise ValueError("AI trace event evidence exceeds the diagnostic bound")
            return [trace.model_copy(deep=True) for trace in traces]

    async def save_meal(self, *, account_id: str, meal: MealEntry) -> MealEntry:
        if meal.account_id != account_id:
            raise CrossAccountAccess
        async with self._lock:
            self._require_active_account_locked(account_id)
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
            if self._account_status_by_id.get(account_id) != "active":
                raise AccountNotProvisioned
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
        pattern_claim: KnowledgeClaim | None = None,
        observation_started_at: datetime | None = None,
        observation_ended_at: datetime | None = None,
        supporting_examples: list[PatternEvidenceExample] | None = None,
        counterexamples: list[PatternEvidenceExample] | None = None,
        prompt_version: str | None = None,
        uncertainty: str | None = None,
    ) -> ClarificationQuestion:
        async with self._lock:
            if (
                account_id not in self._accounts
                or self._account_status_by_id.get(account_id) != "active"
            ):
                raise AccountNotProvisioned
            rich_values = (
                pattern_claim,
                observation_started_at,
                observation_ended_at,
                supporting_examples,
                counterexamples,
                prompt_version,
            )
            rich = any(value is not None for value in rich_values)
            if rich and any(value is None for value in rich_values):
                raise ValueError("rich pattern proposals require complete metadata")
            if rich:
                assert pattern_claim is not None
                assert observation_started_at is not None
                assert observation_ended_at is not None
                assert supporting_examples is not None
                assert counterexamples is not None
                assert prompt_version is not None
                topic_key = pattern_topic_key(pattern_claim)
                evidence_hash = pattern_evidence_hash(
                    observation_started_at=observation_started_at,
                    observation_ended_at=observation_ended_at,
                    supporting_examples=supporting_examples,
                    counterexamples=counterexamples,
                )
                question_id = rich_pattern_question_id(
                    account_id=account_id,
                    topic_key=topic_key,
                    evidence_hash=evidence_hash,
                )
            else:
                topic_key = None
                evidence_hash = None
                question_id = pattern_question_id(account_id, tentative_claim)
            existing = self._questions.get(question_id)
            if existing is not None:
                if existing.account_id != account_id:
                    raise QuestionNotFound
                return existing.model_copy(deep=True)
            predecessor = None
            if topic_key is not None:
                predecessor_id = self._latest_pattern_question_by_topic.get((account_id, topic_key))
                predecessor = (
                    self._questions.get(predecessor_id) if predecessor_id is not None else None
                )
                if predecessor is not None:
                    if predecessor.status == QuestionStatus.OPEN:
                        return predecessor.model_copy(deep=True)
                    if predecessor.response_kind != QuestionResponseKind.REJECT:
                        return predecessor.model_copy(deep=True)
                    prior_support_ids = {
                        item.evidence.id for item in predecessor.pattern_supporting_examples
                    }
                    new_support_ids = {item.evidence.id for item in supporting_examples or []}
                    if (
                        len(new_support_ids - prior_support_ids)
                        < PATTERN_RESURFACE_MINIMUM_NEW_SUPPORT
                        or predecessor.pattern_observation_ended_at is None
                        or observation_ended_at is None
                        or observation_ended_at <= predecessor.pattern_observation_ended_at
                    ):
                        return predecessor.model_copy(deep=True)
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
                pattern_claim=pattern_claim,
                pattern_observation_started_at=observation_started_at,
                pattern_observation_ended_at=observation_ended_at,
                pattern_supporting_examples=supporting_examples or [],
                pattern_counterexamples=counterexamples or [],
                pattern_prompt_version=prompt_version,
                pattern_uncertainty=uncertainty,
                pattern_evidence_hash=evidence_hash,
                pattern_topic_key=topic_key,
                predecessor_question_id=(predecessor.id if predecessor is not None else None),
                created_at=now,
            )
            if superseded is not None:
                superseded.status = QuestionStatus.SUPERSEDED
                superseded.superseded_by_question_id = question.id
                superseded.superseded_at = now
            self._questions[question.id] = question
            if topic_key is not None:
                self._latest_pattern_question_by_topic[(account_id, topic_key)] = question.id
            return question.model_copy(deep=True)

    async def list_meals(self, owner_user_id: str) -> list[MealEntry]:
        return [
            meal
            for meal in await self.list_activity_history(owner_user_id)
            if meal.status != MealStatus.NOT_COOKING
        ]

    async def list_activity_history(
        self,
        owner_user_id: str,
        *,
        status: MealStatus | None = None,
    ) -> list[MealEntry]:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            meals: Iterable[MealEntry] = (
                meal
                for meal in self._meals.values()
                if meal.account_id == account.id and (status is None or meal.status == status)
            )
            return [
                meal.model_copy(deep=True)
                for meal in sorted(
                    meals,
                    key=lambda item: (item.occurred_at or item.created_at, item.id),
                    reverse=True,
                )
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
            if (
                account_id not in self._accounts
                or self._account_status_by_id.get(account_id) != "active"
            ):
                raise AccountNotProvisioned
            duplicate = self._knowledge_revision_requests.get((account_id, idempotency_key))
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

    async def knowledge_revision_result_for_request(
        self,
        *,
        account_id: str,
        idempotency_key: str,
    ) -> KnowledgeRevisionResult | None:
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            stored = self._knowledge_revision_requests.get((account_id, idempotency_key))
            return stored[1].model_copy(deep=True) if stored is not None else None

    async def current_knowledge_revision(
        self,
        *,
        account_id: str,
        topic_key: str,
    ) -> KnowledgeRevisionResult | None:
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            page_id = knowledge_page_id(account_id, topic_key)
            page = self._knowledge_pages.get(page_id)
            if page is None:
                return None
            revision = self._knowledge_revisions[page_id][-1]
            return KnowledgeRevisionResult(
                page=page.model_copy(deep=True),
                revision=revision.model_copy(deep=True),
            )

    async def knowledge_page_index_for_account(
        self,
        *,
        account_id: str,
        limit: int = 50,
    ) -> list[KnowledgePage]:
        if not 1 <= limit <= 100:
            raise ValueError("knowledge page limit must be between 1 and 100")
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            pages = (
                page
                for page in self._knowledge_pages.values()
                if page.account_id == account_id and page.lifecycle != KnowledgeLifecycle.RETIRED
            )
            return [
                page.model_copy(deep=True)
                for page in sorted(
                    pages,
                    key=lambda item: (item.updated_at, item.id),
                    reverse=True,
                )[:limit]
            ]

    async def active_knowledge_revision_for_account(
        self,
        *,
        account_id: str,
        page_id: str,
    ) -> KnowledgeRevisionResult:
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            page = self._knowledge_pages.get(page_id)
            if (
                page is None
                or page.account_id != account_id
                or page.lifecycle == KnowledgeLifecycle.RETIRED
            ):
                raise KnowledgePageNotFound
            revision = self._knowledge_revisions[page.id][-1]
            if (
                revision.account_id != account_id
                or revision.page_id != page.id
                or revision.id != page.current_revision_id
            ):
                raise KnowledgePageNotFound
            return KnowledgeRevisionResult(
                page=page.model_copy(deep=True),
                revision=revision.model_copy(deep=True),
            )

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

    async def list_knowledge_pages_for_owner(
        self,
        owner_user_id: str,
        *,
        include_retired: bool = False,
        limit: int = 50,
    ) -> list[KnowledgePage]:
        if not 1 <= limit <= 100:
            raise ValueError("knowledge page limit must be between 1 and 100")
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            pages = [
                page
                for page in self._knowledge_pages.values()
                if page.account_id == account.id
                and (include_retired or page.lifecycle != KnowledgeLifecycle.RETIRED)
            ]
            return [
                page.model_copy(deep=True)
                for page in sorted(
                    pages,
                    key=lambda item: (item.updated_at, item.id),
                    reverse=True,
                )[:limit]
            ]

    async def list_knowledge_revisions(
        self,
        owner_user_id: str,
        page_id: str,
    ) -> list[KnowledgeRevision]:
        await self.knowledge_page_for_owner(owner_user_id, page_id)
        async with self._lock:
            return [
                revision.model_copy(deep=True) for revision in self._knowledge_revisions[page_id]
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
            if self._account_status_by_id.get(account.id) != "active":
                raise AccountNotProvisioned
            meal = self._owned_meal(account.id, meal_id)
            return self._record_feedback_locked(
                account_id=account.id,
                meal=meal,
                request=request,
                idempotency_key=idempotency_key,
            )

    async def list_meal_feedback_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 200,
    ) -> list[MealFeedbackView]:
        if not 1 <= limit <= 200:
            raise ValueError("feedback list limit must be between 1 and 200")
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            feedback = sorted(
                (item for item in self._feedback.values() if item.account_id == account.id),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
            return [
                MealFeedbackView.model_validate(item.model_dump(exclude={"idempotency_key"}))
                for item in feedback[:limit]
            ]

    async def create_user_context_note(
        self,
        *,
        owner_user_id: str,
        request: UserContextNoteCreate,
        idempotency_key: str,
    ) -> UserContextNote:
        account = await self.account_for_owner(owner_user_id)
        note_id = user_context_note_id(account.id, idempotency_key)
        request_hash = user_context_note_request_hash(request)
        async with self._lock:
            if self._account_status_by_id.get(account.id) != "active":
                raise AccountNotProvisioned
            existing = self._user_context_notes.get(note_id)
            if existing is not None:
                if self._user_context_note_request_hashes[note_id] != request_hash:
                    raise IdempotencyConflict
                return existing.model_copy(deep=True)
            note = UserContextNote(
                id=note_id,
                account_id=account.id,
                author_user_id=owner_user_id,
                **request.model_dump(mode="python"),
            )
            self._user_context_notes[note.id] = note
            self._user_context_note_request_hashes[note.id] = request_hash
            return note.model_copy(deep=True)

    async def list_user_context_notes(
        self,
        owner_user_id: str,
        *,
        include_inactive: bool = False,
        active_at: datetime | None = None,
    ) -> list[UserContextNote]:
        account = await self.account_for_owner(owner_user_id)
        evaluated_at = active_at or utc_now()
        async with self._lock:
            notes = [
                note
                for note in self._user_context_notes.values()
                if note.account_id == account.id
                and (include_inactive or note.is_active_at(evaluated_at))
            ]
            return [
                note.model_copy(deep=True)
                for note in sorted(
                    notes,
                    key=lambda item: (item.created_at, item.id),
                    reverse=True,
                )
            ]

    async def retire_user_context_note(
        self,
        *,
        owner_user_id: str,
        note_id: str,
    ) -> UserContextNote:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            if self._account_status_by_id.get(account.id) != "active":
                raise AccountNotProvisioned
            note = self._user_context_notes.get(note_id)
            if note is None:
                raise UserContextNoteNotFound
            if note.account_id != account.id:
                raise CrossAccountAccess
            if note.status == UserContextNoteStatus.RETIRED:
                return note.model_copy(deep=True)
            retired = note.model_copy(
                update={
                    "status": UserContextNoteStatus.RETIRED,
                    "retired_at": utc_now(),
                },
                deep=True,
            )
            self._user_context_notes[note_id] = retired
            return retired.model_copy(deep=True)

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

    async def recent_meals_for_account(
        self,
        *,
        account_id: str,
        limit: int = 20,
    ) -> list[MealEntry]:
        if not 1 <= limit <= 100:
            raise ValueError("recent meal limit must be between 1 and 100")
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            meals = [
                meal
                for meal in self._meals.values()
                if meal.account_id == account_id
                and meal.event_id is not None
                and meal.status != MealStatus.NOT_COOKING
            ]
            return [
                meal.model_copy(deep=True)
                for meal in sorted(
                    meals,
                    key=lambda item: (item.occurred_at or item.created_at, item.id),
                    reverse=True,
                )[:limit]
            ]

    async def recent_meal_evidence_for_account(
        self,
        *,
        account_id: str,
        limit: int = 20,
    ) -> list[tuple[MealEntry, MealRevision]]:
        meals = await self.recent_meals_for_account(account_id=account_id, limit=limit)
        async with self._lock:
            result: list[tuple[MealEntry, MealRevision]] = []
            for meal in meals:
                revisions = self._meal_revisions.get(meal.id, [])
                current = next(
                    (
                        revision
                        for revision in reversed(revisions)
                        if revision.number == meal.revision_number
                    ),
                    None,
                )
                if current is None or current.account_id != account_id:
                    raise MealRevisionConflict
                result.append((meal.model_copy(deep=True), current.model_copy(deep=True)))
            return result

    async def active_user_context_notes_for_account(
        self,
        *,
        account_id: str,
        active_at: datetime | None = None,
        limit: int = 20,
    ) -> list[UserContextNote]:
        if not 1 <= limit <= 100:
            raise ValueError("active context limit must be between 1 and 100")
        evaluated_at = active_at or utc_now()
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            notes = [
                note
                for note in self._user_context_notes.values()
                if note.account_id == account_id and note.is_active_at(evaluated_at)
            ]
            return [
                note.model_copy(deep=True)
                for note in sorted(
                    notes,
                    key=lambda item: (item.created_at, item.id),
                    reverse=True,
                )[:limit]
            ]

    async def unresolved_reviews_for_account(
        self,
        *,
        account_id: str,
        limit: int = 20,
    ) -> tuple[list[MealEntry], list[ClarificationQuestion]]:
        if not 1 <= limit <= 100:
            raise ValueError("unresolved review limit must be between 1 and 100")
        async with self._lock:
            if account_id not in self._accounts:
                raise AccountNotProvisioned
            meals = sorted(
                (
                    meal
                    for meal in self._meals.values()
                    if meal.account_id == account_id
                    and meal.status in {MealStatus.PROVISIONAL, MealStatus.CONTRADICTED}
                ),
                key=lambda item: (item.occurred_at or item.created_at, item.id),
                reverse=True,
            )[:limit]
            questions = sorted(
                (
                    question
                    for question in self._questions.values()
                    if question.account_id == account_id and question.status == QuestionStatus.OPEN
                ),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )[:limit]
            return (
                [meal.model_copy(deep=True) for meal in meals],
                [question.model_copy(deep=True) for question in questions],
            )

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
            if self._account_status_by_id.get(account.id) != "active":
                raise AccountNotProvisioned
            question = self._questions.get(question_id)
            if not question or question.account_id != account.id:
                raise QuestionNotFound
            duplicate_id = self._question_response_by_idempotency.get((account.id, idempotency_key))
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
                        request.correction if request.kind == QuestionResponseKind.CORRECT else None
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
                feedback_id=(feedback_result.feedback.id if feedback_result is not None else None),
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

    async def list_question_responses_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 200,
    ) -> list[QuestionResponseView]:
        if not 1 <= limit <= 200:
            raise ValueError("question response list limit must be between 1 and 200")
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            responses = sorted(
                (
                    item
                    for item in self._question_responses.values()
                    if item.account_id == account.id
                ),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
            return [
                QuestionResponseView.model_validate(item.model_dump(exclude={"idempotency_key"}))
                for item in responses[:limit]
            ]

    async def capture_for_owner(self, owner_user_id: str, capture_id: str) -> CaptureRecord:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            capture = self._captures.get(capture_id)
            if not capture:
                raise CaptureNotFound
            if capture.account_id != account.id:
                raise CrossAccountAccess
            return capture.model_copy(deep=True)

    async def recent_captures_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 20,
    ) -> list[CaptureRecord]:
        if not 1 <= limit <= 200:
            raise ValueError("capture list limit must be between 1 and 200")
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            captures = sorted(
                (
                    capture
                    for capture in self._captures.values()
                    if capture.account_id == account.id
                ),
                key=lambda capture: (capture.created_at, capture.id),
                reverse=True,
            )
            return [capture.model_copy(deep=True) for capture in captures[:limit]]

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
                or feedback.learning_disposition != request.learning_disposition
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
            learning_disposition=request.learning_disposition,
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
            occurred_utc_offset_minutes=meal.occurred_utc_offset_minutes,
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
        if request.kind == MealFeedbackKind.NOT_COOKING:
            question_id = self._question_by_meal.get(meal.id)
            question = self._questions.get(question_id) if question_id is not None else None
            if question is not None and question.status == QuestionStatus.OPEN:
                question.status = QuestionStatus.SUPERSEDED
                question.superseded_at = feedback.created_at
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
    if request.kind == MealFeedbackKind.NOT_COOKING:
        if meal.status == MealStatus.NOT_COOKING:
            raise InvalidMealFeedbackTransition
        return inference_from_meal(meal), MealStatus.NOT_COOKING

    if meal.status == MealStatus.NOT_COOKING and (
        request.kind != MealFeedbackKind.CORRECT
        or (request.actual_meal is None and request.correction is None)
    ):
        raise InvalidMealFeedbackTransition

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
        component.preparation_methods[correction.preparation_method_index] = correction.replacement
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
