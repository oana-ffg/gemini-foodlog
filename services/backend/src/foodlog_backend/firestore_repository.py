import asyncio
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1 import DocumentSnapshot
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from .audit import build_audit_event
from .errors import (
    AccountAlreadyProvisioned,
    AccountCapacityReached,
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
    PurchaseIdentityConflict,
    PurchaseNormalizationConflict,
    PurchaseNotFound,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    QuestionSuperseded,
    RawMailNotFound,
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
from .inference_schema import ActivityMealInferenceV1
from .models import (
    Account,
    AccountCreatedOutbox,
    ActivityEvent,
    ActivityEventStatus,
    ActivitySegment,
    AiTraceRecord,
    AuditAction,
    AuditActorKind,
    AuditEvent,
    AuditSource,
    BrowserCamera,
    Camera,
    CameraStatus,
    CaptureEnvelopeV1,
    CaptureRecord,
    CaptureStatus,
    ClarificationQuestion,
    ConsentPreferences,
    DeviceCamera,
    DeviceCredentialRecord,
    DeviceCredentialStatus,
    DurableJob,
    EntitlementMode,
    InboundMailAddress,
    InboundMailRoute,
    JobKind,
    JobStatus,
    KnowledgeClaim,
    KnowledgeLifecycle,
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeRevisionDraft,
    KnowledgeRevisionResult,
    LaunchMailConsent,
    MealEntry,
    MealFeedback,
    MealFeedbackKind,
    MealFeedbackRequest,
    MealFeedbackResult,
    MealFeedbackView,
    MealRevision,
    MealRevisionSource,
    MealStatus,
    ModelSpendReservation,
    ModelUsageRecord,
    NotificationOutboxStatus,
    ParsedPurchaseDocument,
    PatternEvidenceExample,
    Purchase,
    PurchaseCharge,
    PurchaseDocument,
    PurchaseDocumentCandidate,
    PurchaseDocumentKind,
    PurchaseDocumentNormalization,
    PurchaseEvidenceBundle,
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
    UserContextNote,
    UserContextNoteCreate,
    UserContextNoteStatus,
    VerifiedDeviceIdentity,
    WaitlistEntry,
    capture_grouping_job_id,
    event_inference_job_id,
    utc_now,
)
from .purchase_normalization import (
    materialize_purchase_document_normalization,
    purchase_item_id,
    reconcile_purchase_items,
)
from .repository import (
    PATTERN_RESURFACE_MINIMUM_NEW_SUPPORT,
    event_question_from_hypothesis,
    event_question_id,
    inference_from_meal,
    knowledge_page_id,
    knowledge_revision_request_hash,
    materialize_activity_hypothesis,
    materialize_knowledge_page,
    normalize_knowledge_topic,
    pattern_evidence_hash,
    pattern_question_id,
    pattern_topic_key,
    purchase_identity_aliases,
    revised_inference,
    rich_pattern_question_id,
    user_context_note_id,
    user_context_note_request_hash,
    validate_capture_scope,
    validate_enqueueable_job,
    validate_focused_question_prompt,
    validate_knowledge_revision,
    validate_purchase_document_retry,
    validate_purchase_identity_alias,
    validate_purchase_list_limit,
)

ENTITLEMENT_MODE_VALUES = frozenset(item.value for item in EntitlementMode)
PUBLIC_CAPACITY_TRANSACTION_MAX_ATTEMPTS = 10
PUBLIC_CAPACITY_OUTER_RETRY_ATTEMPTS = 4
KNOWLEDGE_TRANSACTION_MAX_ATTEMPTS = 10
KNOWLEDGE_OUTER_RETRY_ATTEMPTS = 5
TRANSACTION_COMMIT_FAILURE_PREFIX = "Failed to commit transaction in "


def _public_capacity_values(
    snapshot: DocumentSnapshot,
    *,
    configured_limit: int,
) -> tuple[int, int]:
    count = snapshot.get("active_account_count") if snapshot.exists else 0
    stored_limit = snapshot.get("account_limit") if snapshot.exists else configured_limit
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("Public account capacity count is invalid")
    if not isinstance(stored_limit, int) or isinstance(stored_limit, bool) or stored_limit < 1:
        raise ValueError("Public account capacity limit is invalid")
    return count, min(stored_limit, configured_limit)


def _transaction_retry_delay(identity: str, attempt: int) -> float:
    jitter_bytes = sha256(f"{identity}:{attempt}".encode()).digest()[:2]
    jitter = int.from_bytes(jitter_bytes) / 65535 * 0.05
    return min(0.025 * (2**attempt), 0.2) + jitter


def _document(model: BaseModel, *, exclude: set[str] | None = None) -> dict[str, Any]:
    data = model.model_dump(mode="python", exclude=exclude or set())
    data["schema_version"] = 1
    return data


def _model[ModelT: BaseModel](snapshot: DocumentSnapshot, model_type: type[ModelT]) -> ModelT:
    data = snapshot.to_dict()
    if data is None:
        raise ValueError(f"Document {snapshot.reference.path} has no data")
    data.pop("schema_version", None)
    if "updated_at" not in model_type.model_fields:
        data.pop("updated_at", None)
    return model_type.model_validate(data)


def _user_context_note_from_snapshot(snapshot: DocumentSnapshot) -> UserContextNote:
    data = snapshot.to_dict()
    if data is None:
        raise ValueError(f"Document {snapshot.reference.path} has no data")
    for internal_field in ("schema_version", "request_hash", "updated_at"):
        data.pop(internal_field, None)
    return UserContextNote.model_validate(data)


def _legacy_browser_camera_is_migratable(data: dict[str, Any] | None) -> bool:
    return bool(
        data
        and data.get("kind") == "browser"
        and data.get("client_instance_id_hash") is None
        and (data.get("status") or CameraStatus.ACTIVE.value) == CameraStatus.ACTIVE.value
    )


def _camera_document_is_active(
    data: dict[str, Any],
    *,
    account_id: str,
    kind: str,
) -> bool:
    return (
        data.get("account_id") == account_id
        and data.get("kind") == kind
        and (data.get("status") or CameraStatus.ACTIVE.value) == CameraStatus.ACTIVE.value
    )


class FirestoreRepository:
    """Account-scoped production repository backed by Firestore Native mode."""

    def __init__(
        self,
        *,
        project_id: str,
        public_account_limit: int,
        trial_image_limit: int,
        unlimited_owner_user_ids: set[str] | None = None,
        model_spend_limit_dkk_micros: int = 400_000_000,
        model_spend_ledger_id: str = "model_spend",
        client: AsyncClient | None = None,
    ) -> None:
        if model_spend_limit_dkk_micros < 1:
            raise ValueError("model spend limit must be positive")
        if (
            not model_spend_ledger_id
            or len(model_spend_ledger_id) > 120
            or not model_spend_ledger_id.replace("_", "").isalnum()
        ):
            raise ValueError("model spend ledger ID is invalid")
        self._client = client or AsyncClient(project=project_id)
        self._public_account_limit = public_account_limit
        self._trial_image_limit = trial_image_limit
        self._unlimited_owner_user_ids = frozenset(unlimited_owner_user_ids or set())
        self._model_spend_limit_dkk_micros = model_spend_limit_dkk_micros
        self._model_spend_ledger_id = model_spend_ledger_id

    def _identity(self, owner_user_id: str):
        return self._client.collection("identities").document(owner_user_id)

    def _account(self, account_id: str):
        return self._client.collection("accounts").document(account_id)

    def _entitlement(self, account_id: str):
        return self._account(account_id).collection("entitlements").document("current")

    def _collection(self, account_id: str, name: str):
        return self._account(account_id).collection(name)

    def _outbox(self, event_id: str):
        return self._client.collection("outbox").document(event_id)

    def _model_spend_ledger(self):
        return self._client.collection("system").document(self._model_spend_ledger_id)

    async def provision_account(self, owner_user_id: str) -> Account:
        account_id = str(uuid4())
        created_at = utc_now()
        entitlement_mode = (
            EntitlementMode.UNLIMITED
            if owner_user_id in self._unlimited_owner_user_ids
            else EntitlementMode.TRIAL
        )
        trial_image_limit = (
            self._trial_image_limit if entitlement_mode == EntitlementMode.TRIAL else None
        )

        @firestore.async_transactional
        async def provision(transaction):
            identity_ref = self._identity(owner_user_id)
            capacity_ref = self._client.collection("system").document("public_capacity")
            identity = await identity_ref.get(transaction=transaction)
            if identity.exists:
                existing_id = identity.get("account_id")
                if identity.get("status") != "active" or not isinstance(existing_id, str):
                    raise AccountNotProvisioned
                return await self._account_in_transaction(
                    transaction,
                    existing_id,
                    expected_owner_user_id=owner_user_id,
                )

            count = 0
            limit = self._public_account_limit
            if entitlement_mode == EntitlementMode.TRIAL:
                capacity = await capacity_ref.get(transaction=transaction)
                count, limit = _public_capacity_values(
                    capacity,
                    configured_limit=self._public_account_limit,
                )
                if count >= limit:
                    raise AccountCapacityReached

            account = Account(
                id=account_id,
                owner_user_id=owner_user_id,
                entitlement_mode=entitlement_mode,
                trial_image_limit=trial_image_limit,
                created_at=created_at,
            )
            event = AccountCreatedOutbox(
                id=f"account-created-{account_id}",
                account_id=account_id,
                entitlement_mode=entitlement_mode,
                trial_image_limit=trial_image_limit,
                public_slot_number=(
                    count + 1 if entitlement_mode == EntitlementMode.TRIAL else None
                ),
                created_at=created_at,
            )
            transaction.create(
                self._account(account_id),
                {
                    "schema_version": 1,
                    "id": account_id,
                    "owner_user_id": owner_user_id,
                    "entitlement_mode": entitlement_mode.value,
                    "status": "active",
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
            transaction.create(
                self._entitlement(account_id),
                {
                    "schema_version": 1,
                    "accepted_image_count": 0,
                    "entitlement_mode": entitlement_mode.value,
                    "trial_image_limit": trial_image_limit,
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
            transaction.create(
                identity_ref,
                {
                    "schema_version": 1,
                    "account_id": account_id,
                    "account_class": (
                        "public" if entitlement_mode == EntitlementMode.TRIAL else "internal"
                    ),
                    "status": "active",
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
            transaction.create(self._outbox(event.id), _document(event))
            if entitlement_mode == EntitlementMode.TRIAL:
                transaction.set(
                    capacity_ref,
                    {
                        "schema_version": 1,
                        "active_account_count": count + 1,
                        "account_limit": limit,
                        "waitlist_open": count + 1 >= limit,
                        "updated_at": created_at,
                    },
                )
            return account

        capacity_ref = self._client.collection("system").document("public_capacity")
        for attempt in range(PUBLIC_CAPACITY_OUTER_RETRY_ATTEMPTS):
            try:
                return await provision(
                    self._client.transaction(max_attempts=PUBLIC_CAPACITY_TRANSACTION_MAX_ATTEMPTS)
                )
            except ValueError as exc:
                if not str(exc).startswith(TRANSACTION_COMMIT_FAILURE_PREFIX):
                    raise

                identity = await self._identity(owner_user_id).get()
                if identity.exists:
                    return await self.account_for_owner(owner_user_id)
                if entitlement_mode == EntitlementMode.TRIAL:
                    capacity = await capacity_ref.get()
                    count, limit = _public_capacity_values(
                        capacity,
                        configured_limit=self._public_account_limit,
                    )
                    if count >= limit:
                        raise AccountCapacityReached from None
                if attempt + 1 == PUBLIC_CAPACITY_OUTER_RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(_transaction_retry_delay(owner_user_id, attempt))

        raise AssertionError("Public capacity retry loop did not return or raise")

    async def _claim_account_notification(
        self,
        *,
        event_id: str,
        ready_status: NotificationOutboxStatus,
        active_status: NotificationOutboxStatus,
        attempt_field: str,
        lease_id: str,
        lease_expires_at: datetime,
    ) -> AccountCreatedOutbox | None:
        event_ref = self._outbox(event_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def claim(transaction):
            snapshot = await event_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            event = _model(snapshot, AccountCreatedOutbox)
            now = utc_now()
            if (
                event.status == active_status
                and event.lease_expires_at is not None
                and event.lease_expires_at > now
            ):
                return None
            if event.status not in {ready_status, active_status}:
                return None
            attempt_count = getattr(event, attempt_field) + 1
            transaction.update(
                event_ref,
                {
                    "status": active_status.value,
                    attempt_field: attempt_count,
                    "lease_id": lease_id,
                    "lease_expires_at": lease_expires_at,
                    "last_error_code": None,
                    "updated_at": now,
                },
            )
            return event.model_copy(
                update={
                    "status": active_status,
                    attempt_field: attempt_count,
                    "lease_id": lease_id,
                    "lease_expires_at": lease_expires_at,
                    "last_error_code": None,
                }
            )

        return await claim(transaction)

    async def _transition_account_notification(
        self,
        *,
        event_id: str,
        lease_id: str,
        expected_status: NotificationOutboxStatus,
        target_status: NotificationOutboxStatus,
        updates: dict[str, Any],
    ) -> bool:
        event_ref = self._outbox(event_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def transition(transaction):
            snapshot = await event_ref.get(transaction=transaction)
            if not snapshot.exists:
                return False
            event = _model(snapshot, AccountCreatedOutbox)
            if event.status != expected_status or event.lease_id != lease_id:
                return False
            transaction.update(
                event_ref,
                {
                    "status": target_status.value,
                    "lease_id": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                    **updates,
                },
            )
            return True

        return await transition(transaction)

    async def claim_account_notification_for_publish(
        self,
        *,
        account_id: str,
        lease_id: str,
        lease_expires_at: datetime,
    ) -> AccountCreatedOutbox | None:
        return await self._claim_account_notification(
            event_id=f"account-created-{account_id}",
            ready_status=NotificationOutboxStatus.PENDING,
            active_status=NotificationOutboxStatus.PUBLISHING,
            attempt_field="publish_attempt_count",
            lease_id=lease_id,
            lease_expires_at=lease_expires_at,
        )

    async def mark_account_notification_published(
        self,
        *,
        event_id: str,
        lease_id: str,
        provider_message_id: str,
    ) -> bool:
        return await self._transition_account_notification(
            event_id=event_id,
            lease_id=lease_id,
            expected_status=NotificationOutboxStatus.PUBLISHING,
            target_status=NotificationOutboxStatus.PUBLISHED,
            updates={
                "provider_message_id": provider_message_id,
                "published_at": utc_now(),
            },
        )

    async def release_account_notification_publish(
        self,
        *,
        event_id: str,
        lease_id: str,
        error_code: str,
    ) -> bool:
        return await self._transition_account_notification(
            event_id=event_id,
            lease_id=lease_id,
            expected_status=NotificationOutboxStatus.PUBLISHING,
            target_status=NotificationOutboxStatus.PENDING,
            updates={"last_error_code": error_code},
        )

    async def claim_account_notification_for_delivery(
        self,
        *,
        event_id: str,
        lease_id: str,
        lease_expires_at: datetime,
    ) -> AccountCreatedOutbox | None:
        return await self._claim_account_notification(
            event_id=event_id,
            ready_status=NotificationOutboxStatus.PUBLISHED,
            active_status=NotificationOutboxStatus.DELIVERING,
            attempt_field="delivery_attempt_count",
            lease_id=lease_id,
            lease_expires_at=lease_expires_at,
        )

    async def mark_account_notification_delivered(
        self,
        *,
        event_id: str,
        lease_id: str,
        provider_delivery_id: str,
    ) -> bool:
        return await self._transition_account_notification(
            event_id=event_id,
            lease_id=lease_id,
            expected_status=NotificationOutboxStatus.DELIVERING,
            target_status=NotificationOutboxStatus.DELIVERED,
            updates={
                "delivered_at": utc_now(),
                "provider_delivery_id": provider_delivery_id,
            },
        )

    async def release_account_notification_delivery(
        self,
        *,
        event_id: str,
        lease_id: str,
        error_code: str,
    ) -> bool:
        return await self._transition_account_notification(
            event_id=event_id,
            lease_id=lease_id,
            expected_status=NotificationOutboxStatus.DELIVERING,
            target_status=NotificationOutboxStatus.PUBLISHED,
            updates={"last_error_code": error_code},
        )

    async def account_for_owner(self, owner_user_id: str) -> Account:
        identity = await self._identity(owner_user_id).get()
        if not identity.exists or identity.get("status") != "active":
            raise AccountNotProvisioned
        account_id = identity.get("account_id")
        account = await self._account(account_id).get()
        entitlement = await self._entitlement(account_id).get()
        if not account.exists or not entitlement.exists:
            raise AccountNotProvisioned
        return self._account_from_snapshots(account, entitlement)

    async def create_inbound_mail_address(
        self,
        *,
        owner_user_id: str,
        address: str,
        address_hash: str,
    ) -> InboundMailAddress:
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def create(transaction):
            identity = await self._identity(owner_user_id).get(transaction=transaction)
            if not identity.exists or identity.get("status") != "active":
                raise AccountNotProvisioned
            account_id = identity.get("account_id")
            if not isinstance(account_id, str) or not account_id:
                raise AccountNotProvisioned

            address_ref = self._collection(account_id, "inbound_mail_addresses").document("current")
            address_snapshot = await address_ref.get(transaction=transaction)
            if address_snapshot.exists:
                existing = _model(address_snapshot, InboundMailAddress)
                existing_hash = sha256(existing.address.casefold().encode()).hexdigest()
                route_snapshot = (
                    await self._client.collection("inbound_mail_routes")
                    .document(existing_hash)
                    .get(transaction=transaction)
                )
                if not route_snapshot.exists:
                    raise InboundAddressStateConflict
                route = _model(route_snapshot, InboundMailRoute)
                if route.account_id != account_id or route.address_id != existing.id:
                    raise InboundAddressStateConflict
                return existing

            route_ref = self._client.collection("inbound_mail_routes").document(address_hash)
            route_snapshot = await route_ref.get(transaction=transaction)
            if route_snapshot.exists:
                route = _model(route_snapshot, InboundMailRoute)
                if route.account_id == account_id:
                    raise InboundAddressStateConflict
                raise InboundAddressCollision

            created_at = utc_now()
            inbound_address = InboundMailAddress(
                account_id=account_id,
                address=address,
                created_at=created_at,
            )
            route = InboundMailRoute(
                id=address_hash,
                account_id=account_id,
                created_at=created_at,
            )
            transaction.create(address_ref, _document(inbound_address))
            transaction.create(route_ref, _document(route))
            return inbound_address

        return await create(transaction)

    async def attach_purchase_document(
        self,
        candidate: PurchaseDocumentCandidate,
    ) -> PurchaseIdentityResult:
        account_ref = self._account(candidate.account_id)
        raw_mail_ref = self._collection(candidate.account_id, "raw_mail").document(
            candidate.raw_mail_id
        )
        document_ref = self._collection(candidate.account_id, "purchase_documents").document(
            candidate.raw_mail_id
        )
        alias_specs = purchase_identity_aliases(candidate)
        alias_refs = [
            (
                alias_id,
                kind,
                reference,
                self._collection(candidate.account_id, "purchase_identities").document(alias_id),
            )
            for alias_id, kind, reference in alias_specs
        ]
        candidate_purchase_id = str(uuid4())
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def attach(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            raw_mail_snapshot = await raw_mail_ref.get(transaction=transaction)
            document_snapshot = await document_ref.get(transaction=transaction)
            if not account_snapshot.exists or account_snapshot.get("status") != "active":
                raise AccountNotProvisioned
            raw_mail_data = raw_mail_snapshot.to_dict() or {}
            if (
                not raw_mail_snapshot.exists
                or raw_mail_data.get("account_id") != candidate.account_id
                or raw_mail_data.get("status") not in {"stored", "published"}
                or raw_mail_data.get("content_sha256") != candidate.raw_content_sha256
            ):
                raise RawMailNotFound

            if document_snapshot.exists:
                document = _model(document_snapshot, PurchaseDocument)
                validate_purchase_document_retry(document, candidate)
                existing_aliases = []
                for alias_id, kind, reference, alias_ref in alias_refs:
                    alias_snapshot = await alias_ref.get(transaction=transaction)
                    if not alias_snapshot.exists:
                        raise PurchaseIdentityConflict
                    alias = _model(alias_snapshot, PurchaseIdentityAlias)
                    validate_purchase_identity_alias(
                        alias,
                        candidate=candidate,
                        alias_id=alias_id,
                        kind=kind,
                        reference=reference,
                    )
                    existing_aliases.append(alias)
                purchase_snapshot = (
                    await self._collection(candidate.account_id, "purchases")
                    .document(document.purchase_id)
                    .get(transaction=transaction)
                )
                if not purchase_snapshot.exists:
                    raise PurchaseIdentityConflict
                purchase = _model(purchase_snapshot, Purchase)
                if (
                    purchase.account_id != candidate.account_id
                    or purchase.merchant != candidate.merchant
                    or any(alias.purchase_id != purchase.id for alias in existing_aliases)
                ):
                    raise PurchaseIdentityConflict
                return PurchaseIdentityResult(
                    purchase=purchase,
                    document=document,
                    duplicate=True,
                )

            alias_snapshots = []
            for alias_id, kind, reference, alias_ref in alias_refs:
                snapshot = await alias_ref.get(transaction=transaction)
                if snapshot.exists:
                    alias = _model(snapshot, PurchaseIdentityAlias)
                    validate_purchase_identity_alias(
                        alias,
                        candidate=candidate,
                        alias_id=alias_id,
                        kind=kind,
                        reference=reference,
                    )
                    alias_snapshots.append((alias, alias_ref))

            purchase_ids = {alias.purchase_id for alias, _ in alias_snapshots}
            if len(purchase_ids) > 1:
                raise PurchaseIdentityConflict
            purchase_id = next(iter(purchase_ids), candidate_purchase_id)
            purchase_ref = self._collection(candidate.account_id, "purchases").document(purchase_id)
            purchase_snapshot = await purchase_ref.get(transaction=transaction)
            now = utc_now()
            if purchase_ids:
                if not purchase_snapshot.exists:
                    raise PurchaseIdentityConflict
                purchase = _model(purchase_snapshot, Purchase)
                if (
                    purchase.account_id != candidate.account_id
                    or purchase.merchant != candidate.merchant
                ):
                    raise PurchaseIdentityConflict
            else:
                if purchase_snapshot.exists:
                    raise PurchaseIdentityConflict
                purchase = Purchase(
                    id=purchase_id,
                    account_id=candidate.account_id,
                    merchant=candidate.merchant,
                    created_at=now,
                    updated_at=now,
                )

            existing_alias_ids = {alias.id for alias, _ in alias_snapshots}
            for alias_id, kind, reference, alias_ref in alias_refs:
                if alias_id not in existing_alias_ids:
                    transaction.create(
                        alias_ref,
                        _document(
                            PurchaseIdentityAlias(
                                id=alias_id,
                                account_id=candidate.account_id,
                                purchase_id=purchase.id,
                                merchant=candidate.merchant,
                                kind=kind,
                                reference_hash=sha256(reference.encode()).hexdigest(),
                                created_at=now,
                            )
                        ),
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
            if purchase_snapshot.exists:
                transaction.update(
                    purchase_ref,
                    {
                        "revision_count": purchase.revision_count,
                        "updated_at": purchase.updated_at,
                        **latest_update,
                    },
                )
            else:
                transaction.create(purchase_ref, _document(purchase))
            transaction.create(document_ref, _document(document))
            return PurchaseIdentityResult(
                purchase=purchase,
                document=document,
                duplicate=False,
            )

        return await attach(transaction)

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
        document_ref = self._collection(document.account_id, "purchase_documents").document(
            document.id
        )
        purchase_ref = self._collection(document.account_id, "purchases").document(
            document.purchase_id
        )
        normalization_ref = self._collection(
            document.account_id, "purchase_normalizations"
        ).document(document.id)
        reconciliation_ref = self._collection(
            document.account_id, "purchase_reconciliations"
        ).document(document.purchase_id)
        transaction = self._client.transaction()

        async def load_items(
            transaction,
            *,
            source_document_id: str | None,
            expected_kind: PurchaseDocumentKind,
        ) -> list[PurchaseItem]:
            if source_document_id is None:
                return []
            if source_document_id == document.id:
                return items
            source_normalization_snapshot = (
                await self._collection(document.account_id, "purchase_normalizations")
                .document(source_document_id)
                .get(transaction=transaction)
            )
            if not source_normalization_snapshot.exists:
                return []
            source_normalization = _model(
                source_normalization_snapshot,
                PurchaseDocumentNormalization,
            )
            if (
                source_normalization.account_id != document.account_id
                or source_normalization.purchase_id != document.purchase_id
                or source_normalization.document_kind != expected_kind
            ):
                raise PurchaseNormalizationConflict
            loaded = []
            for ordinal in range(1, source_normalization.item_count + 1):
                item_snapshot = (
                    await self._collection(document.account_id, "purchase_items")
                    .document(purchase_item_id(source_document_id, ordinal))
                    .get(transaction=transaction)
                )
                if not item_snapshot.exists:
                    raise PurchaseNormalizationConflict
                item = _model(item_snapshot, PurchaseItem)
                if (
                    item.account_id != document.account_id
                    or item.purchase_id != document.purchase_id
                    or item.document_id != source_document_id
                    or item.source_kind != expected_kind
                ):
                    raise PurchaseNormalizationConflict
                loaded.append(item)
            return loaded

        @firestore.async_transactional
        async def normalize(transaction):
            document_snapshot = await document_ref.get(transaction=transaction)
            purchase_snapshot = await purchase_ref.get(transaction=transaction)
            normalization_snapshot = await normalization_ref.get(transaction=transaction)
            reconciliation_snapshot = await reconciliation_ref.get(transaction=transaction)
            if not document_snapshot.exists or not purchase_snapshot.exists:
                raise PurchaseNormalizationConflict
            persisted_document = _model(document_snapshot, PurchaseDocument)
            purchase = _model(purchase_snapshot, Purchase)
            if persisted_document != document or purchase.account_id != document.account_id:
                raise PurchaseNormalizationConflict

            if normalization_snapshot.exists:
                existing = _model(
                    normalization_snapshot,
                    PurchaseDocumentNormalization,
                )
                if (
                    existing.model_dump(exclude={"created_at"})
                    != normalization.model_dump(exclude={"created_at"})
                    or not reconciliation_snapshot.exists
                ):
                    raise PurchaseNormalizationConflict
                persisted_items = []
                for item in items:
                    snapshot = (
                        await self._collection(document.account_id, "purchase_items")
                        .document(item.id)
                        .get(transaction=transaction)
                    )
                    if not snapshot.exists:
                        raise PurchaseNormalizationConflict
                    persisted_items.append(_model(snapshot, PurchaseItem))
                persisted_charges = []
                for charge in charges:
                    snapshot = (
                        await self._collection(document.account_id, "purchase_charges")
                        .document(charge.id)
                        .get(transaction=transaction)
                    )
                    if not snapshot.exists:
                        raise PurchaseNormalizationConflict
                    persisted_charges.append(_model(snapshot, PurchaseCharge))
                return PurchaseNormalizationResult(
                    normalization=existing,
                    items=persisted_items,
                    charges=persisted_charges,
                    reconciliation=_model(
                        reconciliation_snapshot,
                        PurchaseReconciliation,
                    ),
                    duplicate=True,
                )

            confirmation_id = purchase.latest_confirmation_document_id
            final_id = purchase.latest_final_document_id
            if document.kind == PurchaseDocumentKind.ORDER_CONFIRMATION:
                confirmation_id = confirmation_id or document.id
            else:
                final_id = final_id or document.id
            confirmation_items = await load_items(
                transaction,
                source_document_id=confirmation_id,
                expected_kind=PurchaseDocumentKind.ORDER_CONFIRMATION,
            )
            final_items = await load_items(
                transaction,
                source_document_id=final_id,
                expected_kind=PurchaseDocumentKind.FINAL_RECEIPT,
            )
            reconciliation = reconcile_purchase_items(
                account_id=document.account_id,
                purchase_id=document.purchase_id,
                confirmation_document_id=confirmation_id if confirmation_items else None,
                confirmation_items=confirmation_items,
                final_document_id=final_id if final_items else None,
                final_items=final_items,
            )
            transaction.create(normalization_ref, _document(normalization))
            for item in items:
                transaction.create(
                    self._collection(document.account_id, "purchase_items").document(item.id),
                    _document(item),
                )
            for charge in charges:
                transaction.create(
                    self._collection(document.account_id, "purchase_charges").document(charge.id),
                    _document(charge),
                )
            transaction.set(reconciliation_ref, _document(reconciliation))
            return PurchaseNormalizationResult(
                normalization=normalization,
                items=items,
                charges=charges,
                reconciliation=reconciliation,
                duplicate=False,
            )

        return await normalize(transaction)

    async def list_purchases(
        self,
        owner_user_id: str,
        *,
        limit: int = 20,
    ) -> list[Purchase]:
        validate_purchase_list_limit(limit)
        account = await self.account_for_owner(owner_user_id)
        query = (
            self._collection(account.id, "purchases")
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        purchases = [_model(snapshot, Purchase) async for snapshot in query.stream()]
        if any(purchase.account_id != account.id for purchase in purchases):
            raise PurchaseNotFound
        return purchases

    async def purchase_evidence_for_owner(
        self,
        owner_user_id: str,
        purchase_id: str,
    ) -> PurchaseEvidenceBundle:
        account = await self.account_for_owner(owner_user_id)
        purchase_snapshot = await (
            self._collection(account.id, "purchases").document(purchase_id).get()
        )
        if not purchase_snapshot.exists:
            raise PurchaseNotFound
        purchase = _model(purchase_snapshot, Purchase)
        if purchase.account_id != account.id:
            raise PurchaseNotFound

        return await self._purchase_evidence_for_account(
            account_id=account.id,
            purchase=purchase,
        )

    async def recent_purchase_evidence_for_account(
        self,
        *,
        account_id: str,
        limit: int = 5,
    ) -> list[PurchaseEvidenceBundle]:
        validate_purchase_list_limit(limit)
        account = await self._account(account_id).get()
        if not account.exists or account.get("status") != "active":
            raise AccountNotProvisioned
        query = (
            self._collection(account_id, "purchases")
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        purchases = [_model(snapshot, Purchase) async for snapshot in query.stream()]
        if any(purchase.account_id != account_id for purchase in purchases):
            raise CrossAccountAccess
        return list(
            await asyncio.gather(
                *(
                    self._purchase_evidence_for_account(
                        account_id=account_id,
                        purchase=purchase,
                    )
                    for purchase in purchases
                )
            )
        )

    async def _purchase_evidence_for_account(
        self,
        *,
        account_id: str,
        purchase: Purchase,
    ) -> PurchaseEvidenceBundle:
        if purchase.account_id != account_id:
            raise CrossAccountAccess

        async def purchase_collection_models(name: str, model_type):
            query = self._collection(account_id, name).where(
                filter=FieldFilter("purchase_id", "==", purchase.id)
            )
            return [_model(snapshot, model_type) async for snapshot in query.stream()]

        documents = await purchase_collection_models("purchase_documents", PurchaseDocument)
        normalizations = await purchase_collection_models(
            "purchase_normalizations", PurchaseDocumentNormalization
        )
        items = await purchase_collection_models("purchase_items", PurchaseItem)
        charges = await purchase_collection_models("purchase_charges", PurchaseCharge)
        reconciliation_snapshot = await (
            self._collection(account_id, "purchase_reconciliations").document(purchase.id).get()
        )
        reconciliation = (
            _model(reconciliation_snapshot, PurchaseReconciliation)
            if reconciliation_snapshot.exists
            else None
        )
        document_revisions = {document.id: document.revision_number for document in documents}
        return PurchaseEvidenceBundle(
            purchase=purchase,
            documents=sorted(documents, key=lambda document: document.revision_number),
            normalizations=sorted(
                normalizations,
                key=lambda normalization: normalization.document_revision_number,
            ),
            items=sorted(
                items,
                key=lambda item: (item.document_revision_number, item.ordinal),
            ),
            charges=sorted(
                charges,
                key=lambda charge: (
                    document_revisions.get(charge.document_id, 0),
                    charge.kind.value,
                ),
            ),
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
        decision_at = utc_now()
        consent = LaunchMailConsent(
            id=consent_id,
            account_id=account.id,
            actor_user_id=owner_user_id,
            email_normalized=email_normalized,
            granted=granted,
            policy_version=policy_version,
            created_at=decision_at,
        )
        identity_ref = self._identity(owner_user_id)
        consent_ref = self._collection(account.id, "consents").document(consent.id)

        @firestore.async_transactional
        async def record(transaction):
            identity = await identity_ref.get(transaction=transaction)
            existing = await consent_ref.get(transaction=transaction)
            if (
                not identity.exists
                or identity.get("status") != "active"
                or identity.get("account_id") != account.id
            ):
                raise AccountNotProvisioned
            if existing.exists:
                stored = _model(existing, LaunchMailConsent)
            else:
                transaction.create(consent_ref, _document(consent))
                stored = consent
            transaction.update(
                identity_ref,
                {
                    "email_normalized": email_normalized,
                    "mailing_list_opt_in": granted,
                    "mailing_list_policy_version": policy_version,
                    "mailing_list_updated_at": decision_at,
                    "updated_at": decision_at,
                },
            )
            return stored

        return await record(self._client.transaction())

    async def join_waitlist(
        self,
        *,
        firebase_uid: str,
        email_normalized: str,
        policy_version: str,
    ) -> WaitlistEntry:
        entry_id = sha256(firebase_uid.encode()).hexdigest()
        now = utc_now()
        entry = WaitlistEntry(
            id=entry_id,
            email_normalized=email_normalized,
            policy_version=policy_version,
            created_at=now,
            updated_at=now,
        )
        identity_ref = self._identity(firebase_uid)
        waitlist_ref = self._client.collection("waitlist").document(entry.id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def join(transaction):
            identity = await identity_ref.get(transaction=transaction)
            existing = await waitlist_ref.get(transaction=transaction)
            capacity = (
                await self._client.collection("system")
                .document("public_capacity")
                .get(transaction=transaction)
            )
            if identity.exists:
                raise AccountAlreadyProvisioned
            if existing.exists:
                stored = _model(existing, WaitlistEntry)
                if stored.status == "active":
                    if (
                        stored.email_normalized == email_normalized
                        and stored.policy_version == policy_version
                    ):
                        return stored
                    refreshed = stored.model_copy(
                        update={
                            "email_normalized": email_normalized,
                            "policy_version": policy_version,
                            "updated_at": now,
                        },
                    )
                    transaction.set(waitlist_ref, _document(refreshed))
                    return refreshed
            count, limit = _public_capacity_values(
                capacity,
                configured_limit=self._public_account_limit,
            )
            if count < limit:
                raise WaitlistUnavailable
            if existing.exists:
                reactivated = stored.model_copy(
                    update={
                        "email_normalized": email_normalized,
                        "policy_version": policy_version,
                        "mailing_list_opt_in": True,
                        "status": "active",
                        "updated_at": now,
                    },
                )
                transaction.set(waitlist_ref, _document(reactivated))
                return reactivated
            transaction.create(waitlist_ref, _document(entry))
            return entry

        return await join(transaction)

    async def consent_preferences(
        self,
        *,
        firebase_uid: str,
    ) -> ConsentPreferences:
        identity, waitlist = await asyncio.gather(
            self._identity(firebase_uid).get(),
            self._client.collection("waitlist")
            .document(sha256(firebase_uid.encode()).hexdigest())
            .get(),
        )
        launch_opt_in: bool | None = None
        launch_policy_version: str | None = None
        launch_updated_at: datetime | None = None
        if identity.exists and identity.get("status") == "active":
            identity_data = identity.to_dict() or {}
            raw_opt_in = identity_data.get("mailing_list_opt_in")
            if isinstance(raw_opt_in, bool):
                launch_opt_in = raw_opt_in
            raw_policy_version = identity_data.get("mailing_list_policy_version")
            if isinstance(raw_policy_version, str):
                launch_policy_version = raw_policy_version
            raw_updated_at = identity_data.get("mailing_list_updated_at")
            if isinstance(raw_updated_at, datetime):
                launch_updated_at = raw_updated_at
        stored_waitlist = _model(waitlist, WaitlistEntry) if waitlist.exists else None
        return ConsentPreferences(
            launch_mail_opt_in=launch_opt_in,
            launch_mail_policy_version=launch_policy_version,
            launch_mail_updated_at=launch_updated_at,
            waitlist_status=stored_waitlist.status if stored_waitlist else "not_joined",
            waitlist_policy_version=(stored_waitlist.policy_version if stored_waitlist else None),
            waitlist_updated_at=stored_waitlist.updated_at if stored_waitlist else None,
        )

    async def withdraw_waitlist(
        self,
        *,
        firebase_uid: str,
    ) -> WaitlistEntry:
        waitlist_ref = self._client.collection("waitlist").document(
            sha256(firebase_uid.encode()).hexdigest()
        )
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def withdraw(transaction):
            snapshot = await waitlist_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise WaitlistEntryNotFound
            stored = _model(snapshot, WaitlistEntry)
            if stored.status == "withdrawn":
                return stored
            withdrawn_at = utc_now()
            withdrawn = stored.model_copy(
                update={
                    "email_normalized": None,
                    "mailing_list_opt_in": False,
                    "status": "withdrawn",
                    "updated_at": withdrawn_at,
                    "last_withdrawn_at": withdrawn_at,
                    "withdrawal_count": stored.withdrawal_count + 1,
                },
            )
            transaction.set(waitlist_ref, _document(withdrawn))
            return withdrawn

        return await withdraw(transaction)

    async def issue_device_camera(
        self,
        *,
        owner_user_id: str,
        name: str,
        credential_hash: str,
        token_version: int,
    ) -> DeviceCamera:
        account = await self.account_for_owner(owner_user_id)
        camera = DeviceCamera(
            id=str(uuid4()),
            account_id=account.id,
            name=name,
        )
        account_ref = self._account(account.id)
        camera_ref = self._collection(account.id, "cameras").document(camera.id)
        credential_ref = self._client.collection("device_credentials").document(credential_hash)
        credential = DeviceCredentialRecord(
            credential_hash=credential_hash,
            account_id=account.id,
            camera_id=camera.id,
            token_version=token_version,
        )
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def issue(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            existing_credential = await credential_ref.get(transaction=transaction)
            if (
                not account_snapshot.exists
                or account_snapshot.get("status") != "active"
                or account_snapshot.get("owner_user_id") != owner_user_id
            ):
                raise AccountNotProvisioned
            if existing_credential.exists:
                raise DeviceCredentialCollision
            transaction.create(camera_ref, _document(camera))
            transaction.create(credential_ref, _document(credential))
            return camera

        return await issue(transaction)

    async def authenticate_device(
        self,
        credential_hash: str,
    ) -> VerifiedDeviceIdentity:
        credential_ref = self._client.collection("device_credentials").document(credential_hash)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def authenticate(transaction):
            credential_snapshot = await credential_ref.get(transaction=transaction)
            if not credential_snapshot.exists:
                raise InvalidDeviceCredential
            credential = _model(credential_snapshot, DeviceCredentialRecord)
            now = utc_now()
            if credential.status != DeviceCredentialStatus.ACTIVE or (
                credential.expires_at is not None and credential.expires_at <= now
            ):
                raise InvalidDeviceCredential
            account_ref = self._account(credential.account_id)
            camera_ref = self._collection(
                credential.account_id,
                "cameras",
            ).document(credential.camera_id)
            account_snapshot = await account_ref.get(transaction=transaction)
            camera_snapshot = await camera_ref.get(transaction=transaction)
            if (
                not account_snapshot.exists
                or account_snapshot.get("status") != "active"
                or not camera_snapshot.exists
                or camera_snapshot.get("kind") != "device"
                or camera_snapshot.get("status") != CameraStatus.ACTIVE.value
            ):
                raise InvalidDeviceCredential
            owner_user_id = account_snapshot.get("owner_user_id")
            if not isinstance(owner_user_id, str) or not owner_user_id:
                raise InvalidDeviceCredential
            transaction.update(
                credential_ref,
                {"last_used_at": now, "updated_at": now},
            )
            return VerifiedDeviceIdentity(
                owner_user_id=owner_user_id,
                account_id=credential.account_id,
                camera_id=credential.camera_id,
            )

        return await authenticate(transaction)

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
        camera_ref = self._collection(account.id, "cameras").document(camera_id)
        credential_query = self._client.collection("device_credentials").where(
            filter=FieldFilter("camera_id", "==", camera_id)
        )
        credential_refs = [snapshot.reference async for snapshot in credential_query.stream()]
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def revoke(transaction):
            camera_snapshot = await camera_ref.get(transaction=transaction)
            credential_snapshots = [
                await credential_ref.get(transaction=transaction)
                for credential_ref in credential_refs
            ]
            if (
                not camera_snapshot.exists
                or camera_snapshot.get("account_id") != account.id
                or (expected_kind is not None and camera_snapshot.get("kind") != expected_kind)
            ):
                raise CameraNotFound
            model_type = BrowserCamera if camera_snapshot.get("kind") == "browser" else DeviceCamera
            camera = _model(camera_snapshot, model_type)
            if camera.status == CameraStatus.REVOKED:
                return camera
            now = utc_now()
            revoked = camera.model_copy(
                update={
                    "status": CameraStatus.REVOKED,
                    "revoked_at": now,
                }
            )
            transaction.update(
                camera_ref,
                {
                    "status": CameraStatus.REVOKED.value,
                    "revoked_at": now,
                    "updated_at": now,
                },
            )
            if isinstance(camera, DeviceCamera):
                for snapshot in credential_snapshots:
                    if (
                        snapshot.exists
                        and snapshot.get("account_id") == account.id
                        and snapshot.get("camera_id") == camera_id
                    ):
                        transaction.update(
                            snapshot.reference,
                            {
                                "status": DeviceCredentialStatus.REVOKED.value,
                                "revoked_at": now,
                                "updated_at": now,
                            },
                        )
            return revoked

        return await revoke(transaction)

    async def device_camera_for_identity(
        self,
        *,
        account_id: str,
        camera_id: str,
    ) -> DeviceCamera:
        snapshot = await self._collection(account_id, "cameras").document(camera_id).get()
        if (
            not snapshot.exists
            or snapshot.get("account_id") != account_id
            or snapshot.get("kind") != "device"
            or snapshot.get("status") != CameraStatus.ACTIVE.value
        ):
            raise CameraNotFound
        return _model(snapshot, DeviceCamera)

    async def _account_in_transaction(
        self,
        transaction,
        account_id: str,
        *,
        expected_owner_user_id: str,
    ) -> Account:
        account = await self._account(account_id).get(transaction=transaction)
        entitlement = await self._entitlement(account_id).get(transaction=transaction)
        if (
            not account.exists
            or not entitlement.exists
            or account.get("status") != "active"
            or account.get("owner_user_id") != expected_owner_user_id
        ):
            raise AccountNotProvisioned
        return self._account_from_snapshots(account, entitlement)

    @staticmethod
    def _account_from_snapshots(account, entitlement) -> Account:
        entitlement_data = entitlement.to_dict() or {}
        return Account(
            id=account.id,
            owner_user_id=account.get("owner_user_id"),
            entitlement_mode=entitlement_data.get("entitlement_mode") or EntitlementMode.TRIAL,
            trial_image_limit=entitlement_data.get("trial_image_limit"),
            accepted_image_count=entitlement_data.get("accepted_image_count"),
            created_at=account.get("created_at"),
        )

    async def create_browser_camera(
        self,
        owner_user_id: str,
        name: str,
        client_instance_id: str,
    ) -> BrowserCamera:
        account = await self.account_for_owner(owner_user_id)
        account_ref = self._account(account.id)
        instance_hash = sha256(client_instance_id.encode()).hexdigest()
        deterministic_camera_id = f"browser-{instance_hash}"
        matching_cameras = [
            snapshot
            async for snapshot in self._collection(account.id, "cameras")
            .where(filter=FieldFilter("client_instance_id_hash", "==", instance_hash))
            .limit(5)
            .stream()
        ]
        matching_cameras.sort(
            key=lambda snapshot: (snapshot.id != deterministic_camera_id, snapshot.id)
        )
        camera_id = matching_cameras[0].id if matching_cameras else deterministic_camera_id
        if not matching_cameras:
            account_snapshot = await account_ref.get()
            account_data = account_snapshot.to_dict() or {}
            legacy_camera_id = account_data.get("primary_browser_camera_id")
            if isinstance(legacy_camera_id, str) and legacy_camera_id != camera_id:
                legacy_camera = (
                    await self._collection(account.id, "cameras").document(legacy_camera_id).get()
                )
                if legacy_camera.exists and _legacy_browser_camera_is_migratable(
                    legacy_camera.to_dict()
                ):
                    camera_id = legacy_camera.id
        camera_ref = self._collection(account.id, "cameras").document(camera_id)
        created_at = utc_now()
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def create(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            existing = await camera_ref.get(transaction=transaction)
            if (
                not account_snapshot.exists
                or account_snapshot.get("owner_user_id") != owner_user_id
                or account_snapshot.get("status") != "active"
            ):
                raise AccountNotProvisioned
            if existing.exists:
                camera = _model(existing, BrowserCamera)
                model_updates: dict[str, Any] = {}
                if camera.client_instance_id_hash is None:
                    model_updates["client_instance_id_hash"] = instance_hash
                if camera.status == CameraStatus.ACTIVE and camera.name != name:
                    model_updates["name"] = name
                if model_updates:
                    transaction.update(
                        camera_ref,
                        {**model_updates, "updated_at": created_at},
                    )
                    camera = camera.model_copy(update=model_updates, deep=True)
                return camera
            camera = BrowserCamera(
                id=camera_id,
                account_id=account.id,
                name=name,
                client_instance_id_hash=instance_hash,
                created_at=created_at,
            )
            transaction.create(camera_ref, _document(camera))
            return camera

        return await create(transaction)

    async def list_cameras(self, owner_user_id: str) -> list[Camera]:
        account = await self.account_for_owner(owner_user_id)
        cameras: list[Camera] = []
        async for snapshot in self._collection(account.id, "cameras").stream():
            model_type = BrowserCamera if snapshot.get("kind") == "browser" else DeviceCamera
            camera = _model(snapshot, model_type)
            if camera.account_id != account.id:
                raise CrossAccountAccess
            cameras.append(camera)
        return sorted(cameras, key=lambda camera: (camera.created_at, camera.id))

    async def camera_for_owner(self, owner_user_id: str, camera_id: str) -> Camera:
        account = await self.account_for_owner(owner_user_id)
        snapshot = await self._collection(account.id, "cameras").document(camera_id).get()
        if not snapshot.exists:
            raise CameraNotFound
        model_type = BrowserCamera if snapshot.get("kind") == "browser" else DeviceCamera
        camera = _model(snapshot, model_type)
        if camera.account_id != account.id:
            raise CrossAccountAccess
        if camera.status != CameraStatus.ACTIVE:
            raise CameraNotFound
        return camera

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
        key_hash = sha256(idempotency_key.encode()).hexdigest()
        created_at = utc_now()
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
            created_at=created_at,
        )
        capture_ref = self._collection(account.id, "captures").document(capture.id)
        idempotency_ref = self._collection(account.id, "capture_idempotency").document(key_hash)
        account_ref = self._account(account.id)
        camera_ref = self._collection(account.id, "cameras").document(camera.id)
        entitlement_ref = self._entitlement(account.id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def reserve(transaction):
            duplicate = await idempotency_ref.get(transaction=transaction)
            account_snapshot = await account_ref.get(transaction=transaction)
            camera_snapshot = await camera_ref.get(transaction=transaction)
            entitlement = await entitlement_ref.get(transaction=transaction)
            camera_data = camera_snapshot.to_dict() or {}
            if (
                not account_snapshot.exists
                or not entitlement.exists
                or account_snapshot.get("status") != "active"
                or account_snapshot.get("owner_user_id") != account.owner_user_id
            ):
                raise AccountNotProvisioned
            if not camera_snapshot.exists or not _camera_document_is_active(
                camera_data,
                account_id=account.id,
                kind=camera.kind,
            ):
                raise CameraNotFound
            entitlement_data = entitlement.to_dict() or {}
            if duplicate.exists:
                existing = (
                    await self._collection(account.id, "captures")
                    .document(duplicate.get("capture_id"))
                    .get(transaction=transaction)
                )
                if not existing.exists:
                    raise CaptureNotFound
                record = self._capture_from_snapshot(existing, idempotency_key)
                if (
                    record.camera_id != camera.id
                    or record.content_type != content_type
                    or record.content_sha256 != content_sha256
                    or record.metadata != metadata
                ):
                    raise IdempotencyConflict
                return record, self._account_with_entitlement(account, entitlement), False

            count = entitlement_data.get("accepted_image_count")
            mode = entitlement_data.get("entitlement_mode") or EntitlementMode.TRIAL.value
            limit = entitlement_data.get("trial_image_limit")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("Accepted image count is invalid")
            if mode not in ENTITLEMENT_MODE_VALUES:
                raise ValueError("Entitlement mode is invalid")
            if mode == EntitlementMode.TRIAL.value and (
                not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
            ):
                raise ValueError("Trial image limit is invalid")
            if mode == EntitlementMode.TRIAL.value and count >= limit:
                raise TrialQuotaExhausted
            transaction.create(
                capture_ref,
                {
                    **_document(capture, exclude={"idempotency_key"}),
                    "idempotency_hash": key_hash,
                    "received_at": created_at,
                    "updated_at": created_at,
                },
            )
            transaction.create(
                idempotency_ref,
                {
                    "schema_version": 1,
                    "capture_id": capture.id,
                    "camera_id": camera.id,
                    "content_sha256": content_sha256,
                    "content_type": content_type,
                    "state": "reserved",
                    "created_at": created_at,
                },
            )
            transaction.update(
                entitlement_ref,
                {"accepted_image_count": count + 1, "updated_at": created_at},
            )
            updated = account.model_copy(update={"accepted_image_count": count + 1})
            return capture, updated, True

        return await reserve(transaction)

    async def cancel_capture(
        self,
        *,
        account_id: str,
        capture: CaptureRecord,
    ) -> None:
        if capture.account_id != account_id:
            raise CrossAccountAccess
        capture_ref = self._collection(account_id, "captures").document(capture.id)
        entitlement_ref = self._entitlement(account_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def cancel(transaction):
            stored = await capture_ref.get(transaction=transaction)
            entitlement = await entitlement_ref.get(transaction=transaction)
            if not stored.exists:
                return
            if (
                stored.get("account_id") != account_id
                or stored.get("idempotency_hash")
                != sha256(capture.idempotency_key.encode()).hexdigest()
            ):
                raise CaptureNotFound
            key_hash = stored.get("idempotency_hash")
            transaction.delete(capture_ref)
            transaction.delete(
                self._collection(account_id, "capture_idempotency").document(key_hash)
            )
            transaction.update(
                entitlement_ref,
                {
                    "accepted_image_count": max(0, entitlement.get("accepted_image_count") - 1),
                    "updated_at": utc_now(),
                },
            )

        await cancel(transaction)

    async def mark_stored(self, *, account_id: str, capture_id: str) -> None:
        capture_ref = self._collection(account_id, "captures").document(capture_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def store(transaction):
            snapshot = await capture_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise CaptureNotFound
            if snapshot.get("status") != CaptureStatus.ACCEPTED.value:
                return
            camera_ref = self._collection(account_id, "cameras").document(snapshot.get("camera_id"))
            camera_snapshot = await camera_ref.get(transaction=transaction)
            if not camera_snapshot.exists or camera_snapshot.get("account_id") != account_id:
                raise CameraNotFound
            capture_count = camera_snapshot.get("accepted_capture_count") or 0
            if (
                not isinstance(capture_count, int)
                or isinstance(capture_count, bool)
                or capture_count < 0
            ):
                raise ValueError("Accepted camera capture count is invalid")
            key_hash = snapshot.get("idempotency_hash")
            now = utc_now()
            captured_at = snapshot.get("created_at") or now
            previous_capture_at = camera_snapshot.get("last_capture_at")
            job = DurableJob(
                id=capture_grouping_job_id(capture_id),
                account_id=account_id,
                kind=JobKind.CAPTURE_GROUPING,
                subject_id=capture_id,
                subject_revision=1,
                available_at=now,
                created_at=now,
            )
            transaction.update(
                capture_ref,
                {"status": CaptureStatus.STORED.value, "updated_at": now},
            )
            transaction.update(
                camera_ref,
                {
                    "accepted_capture_count": capture_count + 1,
                    "last_capture_at": max(filter(None, (previous_capture_at, captured_at))),
                    "updated_at": now,
                },
            )
            transaction.update(
                self._collection(account_id, "capture_idempotency").document(key_hash),
                {"state": "stored", "updated_at": now},
            )
            transaction.create(
                self._collection(account_id, "jobs").document(job.id),
                _document(job),
            )

        await store(transaction)

    async def mark_processed(self, *, account_id: str, capture_id: str) -> None:
        reference = self._collection(account_id, "captures").document(capture_id)
        snapshot = await reference.get()
        if not snapshot.exists:
            raise CaptureNotFound
        key_hash = snapshot.get("idempotency_hash")
        batch = self._client.batch()
        batch.update(reference, {"status": CaptureStatus.PROCESSED, "updated_at": utc_now()})
        batch.update(
            self._collection(account_id, "capture_idempotency").document(key_hash),
            {"state": "processed"},
        )
        await batch.commit()

    async def capture_for_account(
        self,
        *,
        account_id: str,
        capture_id: str,
    ) -> CaptureRecord:
        snapshot = await self._collection(account_id, "captures").document(capture_id).get()
        if not snapshot.exists:
            raise CaptureNotFound
        capture = self._capture_from_snapshot(snapshot, "")
        if capture.account_id != account_id:
            raise CrossAccountAccess
        return capture

    async def enqueue_job(self, job: DurableJob) -> DurableJob:
        validate_enqueueable_job(job)
        job_ref = self._collection(job.account_id, "jobs").document(job.id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def enqueue(transaction):
            snapshot = await job_ref.get(transaction=transaction)
            if not snapshot.exists:
                transaction.create(job_ref, _document(job))
                return job
            existing = _model(snapshot, DurableJob)
            if existing.kind != job.kind or existing.subject_id != job.subject_id:
                raise JobIdentityConflict
            if job.subject_revision <= existing.subject_revision:
                return existing
            replacement = job.model_copy(update={"created_at": existing.created_at}, deep=True)
            transaction.set(job_ref, _document(replacement))
            return replacement

        return await enqueue(transaction)

    async def job_for_account(self, account_id: str, job_id: str) -> DurableJob | None:
        snapshot = await self._collection(account_id, "jobs").document(job_id).get()
        return _model(snapshot, DurableJob) if snapshot.exists else None

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
        job_ref = self._collection(account_id, "jobs").document(job_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def claim(transaction):
            snapshot = await job_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            job = _model(snapshot, DurableJob)
            now = utc_now()
            if lease_expires_at <= now:
                raise ValueError("Job leases must expire in the future")
            if job.subject_revision != expected_subject_revision:
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
            claimed = DurableJob.model_validate(
                {
                    **job.model_dump(mode="python"),
                    "status": JobStatus.LEASED,
                    "attempt_count": job.attempt_count + 1,
                    "lease_id": lease_id,
                    "lease_owner": lease_owner,
                    "lease_expires_at": lease_expires_at,
                    "last_error_code": None,
                    "last_error_message": None,
                }
            )
            transaction.set(job_ref, _document(claimed))
            return claimed

        return await claim(transaction)

    async def _transition_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
        updates: dict[str, Any],
    ) -> bool:
        job_ref = self._collection(account_id, "jobs").document(job_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def transition(transaction):
            snapshot = await job_ref.get(transaction=transaction)
            if not snapshot.exists:
                return False
            job = _model(snapshot, DurableJob)
            now = utc_now()
            if (
                job.status != JobStatus.LEASED
                or job.subject_revision != expected_subject_revision
                or job.lease_id != lease_id
                or job.lease_owner != lease_owner
                or job.lease_expires_at is None
                or job.lease_expires_at <= now
            ):
                return False
            transitioned = DurableJob.model_validate(
                {
                    **job.model_dump(mode="python"),
                    "lease_id": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    **updates,
                }
            )
            transaction.set(job_ref, _document(transitioned))
            return True

        return await transition(transaction)

    async def complete_job(
        self,
        *,
        account_id: str,
        job_id: str,
        expected_subject_revision: int,
        lease_id: str,
        lease_owner: str,
    ) -> bool:
        now = utc_now()
        return await self._transition_job(
            account_id=account_id,
            job_id=job_id,
            expected_subject_revision=expected_subject_revision,
            lease_id=lease_id,
            lease_owner=lease_owner,
            updates={
                "status": JobStatus.COMPLETED,
                "last_error_code": None,
                "last_error_message": None,
                "completed_at": now,
            },
        )

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
        return await self._transition_job(
            account_id=account_id,
            job_id=job_id,
            expected_subject_revision=expected_subject_revision,
            lease_id=lease_id,
            lease_owner=lease_owner,
            updates={
                "status": JobStatus.PENDING,
                "available_at": available_at,
                "last_error_code": error_code,
                "last_error_message": error_message,
                "completed_at": None,
            },
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
        candidate_event_id = str(uuid4())
        capture_ref = self._collection(account_id, "captures").document(capture_id)
        grouping_job_ref = self._collection(account_id, "jobs").document(
            capture_grouping_job_id(capture_id)
        )
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def group(transaction):
            capture_snapshot = await capture_ref.get(transaction=transaction)
            grouping_job_snapshot = await grouping_job_ref.get(transaction=transaction)
            if not capture_snapshot.exists or not grouping_job_snapshot.exists:
                return None
            capture = self._capture_from_snapshot(capture_snapshot, "internal-grouping")
            grouping_job = _model(grouping_job_snapshot, DurableJob)
            now = utc_now()
            if (
                capture.account_id != account_id
                or capture.status != CaptureStatus.STORED
                or grouping_job.status != JobStatus.LEASED
                or grouping_job.subject_revision != 1
                or grouping_job.lease_id != lease_id
                or grouping_job.lease_owner != lease_owner
                or grouping_job.lease_expires_at is None
                or grouping_job.lease_expires_at <= now
            ):
                return None

            activity_at = capture_activity_time(capture)
            segment_id, source_key = segment_identity(capture)
            segment_ref = self._collection(account_id, "segments").document(segment_id)
            camera_head_ref = self._collection(account_id, "event_heads").document(
                capture.camera_id
            )
            account_head_ref = self._collection(account_id, "event_heads").document(
                ACCOUNT_EVENT_HEAD_ID
            )
            segment_snapshot = await segment_ref.get(transaction=transaction)
            camera_head_snapshot = await camera_head_ref.get(transaction=transaction)
            account_head_snapshot = await account_head_ref.get(transaction=transaction)
            segment = _model(segment_snapshot, ActivitySegment) if segment_snapshot.exists else None

            head_events: dict[str, ActivityEvent] = {}
            for head_snapshot in (camera_head_snapshot, account_head_snapshot):
                head_event_id = head_snapshot.get("event_id") if head_snapshot.exists else None
                if isinstance(head_event_id, str) and head_event_id not in head_events:
                    head_event_snapshot = (
                        await self._collection(account_id, "events")
                        .document(head_event_id)
                        .get(transaction=transaction)
                    )
                    if head_event_snapshot.exists:
                        head_events[head_event_id] = _model(
                            head_event_snapshot,
                            ActivityEvent,
                        )
            camera_head_event_id = (
                camera_head_snapshot.get("event_id") if camera_head_snapshot.exists else None
            )
            account_head_event_id = (
                account_head_snapshot.get("event_id") if account_head_snapshot.exists else None
            )
            camera_head_event = head_events.get(camera_head_event_id)
            account_head_event = head_events.get(account_head_event_id)
            affinity_heads = [
                event
                for event in head_events.values()
                if event.first_capture_at - policy.reopen_window
                <= activity_at
                <= event.last_capture_at + policy.reopen_window
            ]
            affinity_event = (
                max(affinity_heads, key=lambda event: event.last_capture_at)
                if affinity_heads
                else None
            )

            event_created = False
            segment_created = segment is None
            event: ActivityEvent | None = None
            if segment is not None:
                if segment.event_id in head_events:
                    event = head_events[segment.event_id]
                else:
                    segment_event_snapshot = (
                        await self._collection(account_id, "events")
                        .document(segment.event_id)
                        .get(transaction=transaction)
                    )
                    if not segment_event_snapshot.exists:
                        raise ValueError("Segment references a missing activity event")
                    event = _model(segment_event_snapshot, ActivityEvent)
            elif affinity_event is not None:
                event = affinity_event
            else:
                event_created = True
                event = ActivityEvent(
                    id=candidate_event_id,
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

            inference_job_ref = self._collection(account_id, "jobs").document(
                event_inference_job_id(event.id)
            )
            inference_job_snapshot = await inference_job_ref.get(transaction=transaction)
            proposed_inference_job = DurableJob(
                id=inference_job_ref.id,
                account_id=account_id,
                kind=JobKind.EVENT_INFERENCE,
                subject_id=event.id,
                subject_revision=event.current_revision,
                available_at=event.last_capture_at + policy.quiet_after,
                created_at=now,
            )
            if inference_job_snapshot.exists:
                existing_inference_job = _model(inference_job_snapshot, DurableJob)
                if (
                    existing_inference_job.kind != proposed_inference_job.kind
                    or existing_inference_job.subject_id != proposed_inference_job.subject_id
                ):
                    raise JobIdentityConflict
                if proposed_inference_job.subject_revision <= (
                    existing_inference_job.subject_revision
                ):
                    inference_job = existing_inference_job
                else:
                    inference_job = proposed_inference_job.model_copy(
                        update={"created_at": existing_inference_job.created_at},
                        deep=True,
                    )
            else:
                inference_job = proposed_inference_job

            completed_grouping_job = DurableJob.model_validate(
                {
                    **grouping_job.model_dump(mode="python"),
                    "status": JobStatus.COMPLETED,
                    "lease_id": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "completed_at": now,
                }
            )
            event_ref = self._collection(account_id, "events").document(event.id)
            transaction.set(event_ref, _document(event))
            transaction.set(segment_ref, _document(segment))
            transaction.set(inference_job_ref, _document(inference_job))
            transaction.set(grouping_job_ref, _document(completed_grouping_job))
            transaction.update(
                capture_ref,
                {
                    "segment_id": segment.id,
                    "event_id": event.id,
                    "updated_at": now,
                },
            )
            if (
                camera_head_event is None
                or event.last_capture_at >= camera_head_event.last_capture_at
            ):
                transaction.set(
                    camera_head_ref,
                    {
                        "schema_version": 1,
                        "event_id": event.id,
                        "updated_at": now,
                    },
                )
            if (
                account_head_event is None
                or event.last_capture_at >= account_head_event.last_capture_at
            ):
                transaction.set(
                    account_head_ref,
                    {
                        "schema_version": 1,
                        "event_id": event.id,
                        "updated_at": now,
                    },
                )
            return CaptureGroupingResult(
                event=event,
                segment=segment,
                inference_job=inference_job,
                event_created=event_created,
                segment_created=segment_created,
            )

        return await group(transaction)

    async def event_evidence_for_account(
        self,
        *,
        account_id: str,
        event_id: str,
    ) -> tuple[ActivityEvent, list[CaptureRecord]]:
        event_snapshot = await self._collection(account_id, "events").document(event_id).get()
        if not event_snapshot.exists:
            raise ActivityEventNotFound
        event = _model(event_snapshot, ActivityEvent)
        if event.account_id != account_id:
            raise ActivityEventNotFound

        query = self._collection(account_id, "captures").where(
            filter=FieldFilter("event_id", "==", event_id)
        )
        captures = [self._capture_from_snapshot(snapshot, "") async for snapshot in query.stream()]
        if any(capture.account_id != account_id for capture in captures):
            raise ValueError("Activity event evidence escaped its account scope")
        if len(captures) != event.capture_count:
            raise ValueError("Activity event evidence is incomplete")
        return event, sorted(captures, key=capture_evidence_order)

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
        event_ref = self._collection(account_id, "events").document(event_id)
        job_ref = self._collection(account_id, "jobs").document(event_inference_job_id(event_id))
        capture_refs = [
            self._collection(account_id, "captures").document(capture_id)
            for capture_id in hypothesis.source_capture_ids
        ]
        revision_id = str(uuid4())
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def publish(transaction):
            event_snapshot = await event_ref.get(transaction=transaction)
            job_snapshot = await job_ref.get(transaction=transaction)
            if not event_snapshot.exists or not job_snapshot.exists:
                return None
            event = _model(event_snapshot, ActivityEvent)
            job = _model(job_snapshot, DurableJob)
            now = utc_now()
            if (
                event.account_id != account_id
                or event.id != event_id
                or event.current_revision != expected_event_revision
                or event.status != ActivityEventStatus.OPEN
                or job.account_id != account_id
                or job.kind != JobKind.EVENT_INFERENCE
                or job.subject_id != event_id
                or job.subject_revision != expected_event_revision
                or job.status != JobStatus.LEASED
                or job.lease_id != lease_id
                or job.lease_owner != lease_owner
                or job.lease_expires_at is None
                or job.lease_expires_at <= now
            ):
                return None
            if len(capture_refs) != event.capture_count:
                raise ValueError("Activity hypothesis evidence count does not match the event")
            capture_snapshots = [
                await capture_ref.get(transaction=transaction) for capture_ref in capture_refs
            ]
            if any(not snapshot.exists for snapshot in capture_snapshots):
                raise ValueError("Activity hypothesis references missing event evidence")
            captures = [self._capture_from_snapshot(snapshot, "") for snapshot in capture_snapshots]
            if any(
                capture.account_id != account_id or capture.event_id != event_id
                for capture in captures
            ):
                raise ValueError("Activity hypothesis evidence escaped its event scope")
            canonical_captures = sorted(captures, key=capture_evidence_order)

            meal_id = event.meal_id or event.id
            meal_ref = self._collection(account_id, "meals").document(meal_id)
            meal_snapshot = await meal_ref.get(transaction=transaction)
            existing_meal = _model(meal_snapshot, MealEntry) if meal_snapshot.exists else None
            if existing_meal is not None and (
                existing_meal.account_id != account_id or existing_meal.event_id != event_id
            ):
                raise CrossAccountAccess
            revision_number = existing_meal.revision_number + 1 if existing_meal is not None else 1
            created_at = existing_meal.created_at if existing_meal is not None else now
            meal = materialize_activity_hypothesis(
                event=event,
                captures=canonical_captures,
                hypothesis=hypothesis,
                meal_id=meal_id,
                revision_number=revision_number,
                created_at=created_at,
            )
            revision = MealRevision(
                id=revision_id,
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
            previous_question_ref = None
            previous_question = None
            if existing_meal is not None and (
                existing_meal.clarification_question is not None
                or (
                    existing_meal.activity_hypothesis is not None
                    and existing_meal.activity_hypothesis.question is not None
                )
            ):
                previous_question_ref = self._collection(account_id, "questions").document(
                    event_question_id(meal.id, existing_meal.revision_number)
                )
                previous_question_snapshot = await previous_question_ref.get(
                    transaction=transaction
                )
                if previous_question_snapshot.exists:
                    previous_question = _model(
                        previous_question_snapshot,
                        ClarificationQuestion,
                    )
            inferred_event = event.model_copy(
                update={
                    "status": ActivityEventStatus.INFERRED,
                    "meal_id": meal.id,
                    "updated_at": now,
                },
                deep=True,
            )
            completed_job = DurableJob.model_validate(
                {
                    **job.model_dump(mode="python"),
                    "status": JobStatus.COMPLETED,
                    "lease_id": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error_code": None,
                    "last_error_message": None,
                    "completed_at": now,
                }
            )

            transaction.set(meal_ref, _document(meal))
            transaction.create(
                meal_ref.collection("revisions").document(revision.id),
                _document(revision),
            )
            if previous_question is not None and previous_question.status == QuestionStatus.OPEN:
                transaction.update(
                    previous_question_ref,
                    {
                        "status": QuestionStatus.SUPERSEDED,
                        "superseded_by_question_id": question.id if question else None,
                        "superseded_at": now,
                        "updated_at": now,
                    },
                )
            if question is not None:
                transaction.create(
                    self._collection(account_id, "questions").document(question.id),
                    _document(question),
                )
            transaction.set(event_ref, _document(inferred_event))
            transaction.set(job_ref, _document(completed_job))
            for capture_ref in capture_refs:
                transaction.update(
                    capture_ref,
                    {"status": CaptureStatus.PROCESSED, "updated_at": now},
                )
            return meal

        return await publish(transaction)

    async def reserve_model_spend(
        self,
        reservation: ModelSpendReservation,
    ) -> ModelSpendReservation:
        ledger_ref = self._model_spend_ledger()
        reservation_ref = ledger_ref.collection("reservations").document(reservation.id)
        account_ref = self._account(reservation.account_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def reserve(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            ledger_snapshot = await ledger_ref.get(transaction=transaction)
            existing_snapshot = await reservation_ref.get(transaction=transaction)
            if (
                not account_snapshot.exists
                or account_snapshot.get("status") != "active"
                or account_snapshot.get("id") != reservation.account_id
            ):
                raise AccountNotProvisioned
            if existing_snapshot.exists:
                existing = _model(existing_snapshot, ModelSpendReservation)
                if existing.model_dump(exclude={"created_at"}) != reservation.model_dump(
                    exclude={"created_at"}
                ):
                    raise ModelSpendReservationConflict
                return existing

            if ledger_snapshot.exists:
                stored_limit = ledger_snapshot.get("limit_dkk_micros")
                reserved_total = ledger_snapshot.get("reserved_dkk_micros")
                if (
                    not isinstance(stored_limit, int)
                    or isinstance(stored_limit, bool)
                    or stored_limit < 1
                    or not isinstance(reserved_total, int)
                    or isinstance(reserved_total, bool)
                    or reserved_total < 0
                ):
                    raise ValueError("Model spend ledger is invalid")
                effective_limit = min(
                    stored_limit,
                    self._model_spend_limit_dkk_micros,
                )
                created_at = ledger_snapshot.get("created_at")
            else:
                effective_limit = self._model_spend_limit_dkk_micros
                reserved_total = 0
                created_at = reservation.created_at

            proposed_total = reserved_total + reservation.reserved_dkk_micros
            if proposed_total > effective_limit:
                raise ModelSpendLimitExceeded
            transaction.create(reservation_ref, _document(reservation))
            ledger_update = {
                "limit_dkk_micros": effective_limit,
                "reserved_dkk_micros": proposed_total,
                "updated_at": reservation.created_at,
            }
            if ledger_snapshot.exists:
                transaction.update(ledger_ref, ledger_update)
            else:
                transaction.create(
                    ledger_ref,
                    {
                        "schema_version": 1,
                        "currency": "DKK",
                        **ledger_update,
                        "actual_dkk_micros": 0,
                        "reconciled_reservation_count": 0,
                        "created_at": created_at,
                    },
                )
            return reservation

        return await reserve(transaction)

    async def model_usage_for_reservation(
        self,
        *,
        account_id: str,
        reservation_id: str,
    ) -> ModelUsageRecord | None:
        snapshot = await self._collection(account_id, "model_usage").document(reservation_id).get()
        if not snapshot.exists:
            return None
        usage = _model(snapshot, ModelUsageRecord)
        if usage.account_id != account_id or usage.reservation_id != reservation_id:
            raise ModelUsageConflict
        return usage

    async def record_model_usage(self, usage: ModelUsageRecord) -> ModelUsageRecord:
        ledger_ref = self._model_spend_ledger()
        reservation_ref = ledger_ref.collection("reservations").document(usage.reservation_id)
        usage_ref = self._collection(usage.account_id, "model_usage").document(usage.id)
        account_ref = self._account(usage.account_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def record(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            ledger_snapshot = await ledger_ref.get(transaction=transaction)
            reservation_snapshot = await reservation_ref.get(transaction=transaction)
            existing_snapshot = await usage_ref.get(transaction=transaction)
            if (
                not account_snapshot.exists
                or account_snapshot.get("status") != "active"
                or account_snapshot.get("id") != usage.account_id
            ):
                raise AccountNotProvisioned
            if not ledger_snapshot.exists or not reservation_snapshot.exists:
                raise ModelUsageConflict
            reservation = _model(reservation_snapshot, ModelSpendReservation)
            if (
                usage.id != reservation.id
                or usage.account_id != reservation.account_id
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
            if existing_snapshot.exists:
                existing = _model(existing_snapshot, ModelUsageRecord)
                if existing.model_dump(exclude={"created_at"}) != usage.model_dump(
                    exclude={"created_at"}
                ):
                    raise ModelUsageConflict
                return existing

            ledger_data = ledger_snapshot.to_dict() or {}
            actual_total = ledger_data.get("actual_dkk_micros", 0)
            reconciled_count = ledger_data.get("reconciled_reservation_count", 0)
            if (
                not isinstance(actual_total, int)
                or isinstance(actual_total, bool)
                or actual_total < 0
                or not isinstance(reconciled_count, int)
                or isinstance(reconciled_count, bool)
                or reconciled_count < 0
            ):
                raise ValueError("Model spend reconciliation ledger is invalid")
            transaction.create(usage_ref, _document(usage))
            transaction.update(
                ledger_ref,
                {
                    "actual_dkk_micros": actual_total + usage.actual_dkk_micros,
                    "reconciled_reservation_count": reconciled_count + 1,
                    "updated_at": usage.created_at,
                },
            )
            return usage

        return await record(transaction)

    async def record_ai_trace(self, trace: AiTraceRecord) -> AiTraceRecord:
        account_ref = self._account(trace.account_id)
        trace_ref = self._collection(trace.account_id, "traces").document(trace.id)
        audit = build_audit_event(
            account_id=trace.account_id,
            action=AuditAction.AI_TRACE_RECORDED,
            actor_kind=AuditActorKind.SYSTEM,
            source=AuditSource.AGENT,
            subject_kind="trace",
            subject_id=trace.id,
        )
        audit_ref = self._collection(trace.account_id, "audit_events").document(audit.id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def record(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            existing_snapshot = await trace_ref.get(transaction=transaction)
            audit_snapshot = await audit_ref.get(transaction=transaction)
            if (
                not account_snapshot.exists
                or account_snapshot.get("status") != "active"
                or account_snapshot.get("id") != trace.account_id
            ):
                raise AccountNotProvisioned
            if existing_snapshot.exists:
                existing = _model(existing_snapshot, AiTraceRecord)
                if existing != trace:
                    raise AiTraceConflict
                stored = existing
            else:
                transaction.create(trace_ref, _document(trace))
                stored = trace
            if audit_snapshot.exists:
                existing_audit = _model(audit_snapshot, AuditEvent)
                if existing_audit.model_dump(exclude={"created_at"}) != audit.model_dump(
                    exclude={"created_at"}
                ):
                    raise ValueError("audit event identity conflicts with existing evidence")
            else:
                transaction.create(audit_ref, _document(audit))
            return stored

        return await record(transaction)

    async def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        account_ref = self._account(event.account_id)
        event_ref = self._collection(event.account_id, "audit_events").document(event.id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def append(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            existing_snapshot = await event_ref.get(transaction=transaction)
            if (
                not account_snapshot.exists
                or account_snapshot.get("status") != "active"
                or account_snapshot.get("id") != event.account_id
            ):
                raise AccountNotProvisioned
            if existing_snapshot.exists:
                existing = _model(existing_snapshot, AuditEvent)
                if existing.model_dump(exclude={"created_at"}) != event.model_dump(
                    exclude={"created_at"}
                ):
                    raise ValueError("audit event identity conflicts with existing evidence")
                return existing
            transaction.create(event_ref, _document(event))
            return event

        return await append(transaction)

    async def list_audit_events_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 100,
    ) -> list[AuditEvent]:
        if not 1 <= limit <= 200:
            raise ValueError("audit event list limit must be between 1 and 200")
        account = await self.account_for_owner(owner_user_id)
        query = (
            self._collection(account.id, "audit_events")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [_model(snapshot, AuditEvent) async for snapshot in query.stream()]

    async def ai_trace_for_account(
        self,
        *,
        account_id: str,
        trace_id: str,
    ) -> AiTraceRecord:
        snapshot = await self._collection(account_id, "traces").document(trace_id).get()
        if not snapshot.exists:
            raise AiTraceNotFound
        trace = _model(snapshot, AiTraceRecord)
        if trace.account_id != account_id or trace.id != trace_id:
            raise AiTraceNotFound
        return trace

    async def save_meal(self, *, account_id: str, meal: MealEntry) -> MealEntry:
        if meal.account_id != account_id:
            raise CrossAccountAccess
        meal_ref = self._collection(account_id, "meals").document(meal.id)
        capture_ref = self._collection(account_id, "captures").document(meal.capture_id)
        revision = MealRevision(
            id=str(uuid4()),
            account_id=account_id,
            meal_id=meal.id,
            number=1,
            status=meal.status,
            inference=inference_from_meal(meal),
            source=MealRevisionSource.INFERENCE,
            created_at=meal.created_at,
        )
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def save(transaction):
            capture = await capture_ref.get(transaction=transaction)
            if not capture.exists or capture.get("account_id") != account_id:
                raise CaptureNotFound
            existing_id = (capture.to_dict() or {}).get("meal_id")
            if existing_id:
                existing = (
                    await self._collection(account_id, "meals")
                    .document(existing_id)
                    .get(transaction=transaction)
                )
                return _model(existing, MealEntry)
            transaction.create(meal_ref, _document(meal))
            transaction.create(
                meal_ref.collection("revisions").document(revision.id), _document(revision)
            )
            transaction.update(capture_ref, {"meal_id": meal.id, "updated_at": utc_now()})
            return meal

        return await save(transaction)

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
        meal_ref = self._collection(account_id, "meals").document(meal.id)
        meal_snapshot = await meal_ref.get()
        if not meal_snapshot.exists or meal_snapshot.get("account_id") != account_id:
            raise MealNotFound
        stored_meal = _model(meal_snapshot, MealEntry)
        choices = list(dict.fromkeys([stored_meal.title, *stored_meal.alternatives]))[:8]
        if len(choices) < 2:
            raise ValueError("focused event questions require at least two concrete choices")
        revision_query = meal_ref.collection("revisions").where(
            filter=FieldFilter("number", "==", stored_meal.revision_number)
        )
        revision_snapshots = [snapshot async for snapshot in revision_query.stream()]
        if len(revision_snapshots) != 1:
            raise ValueError("meal revision is missing or ambiguous")
        revision = _model(revision_snapshots[0], MealRevision)
        question = ClarificationQuestion(
            id=meal.id,
            account_id=account_id,
            kind=QuestionKind.EVENT_CLARIFICATION,
            meal_id=meal.id,
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
        question_ref = self._collection(account_id, "questions").document(question.id)
        try:
            await question_ref.create(_document(question))
            return question
        except AlreadyExists:
            existing = await question_ref.get()
            if not existing.exists:
                raise QuestionNotFound from None
            stored = _model(existing, ClarificationQuestion)
            if stored.account_id != account_id or stored.meal_id != meal.id:
                raise QuestionNotFound from None
            return stored

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
        )
        question_ref = self._collection(account_id, "questions").document(question.id)
        topic_ref = (
            self._collection(account_id, "pattern_hypothesis_topics").document(topic_key)
            if topic_key is not None
            else None
        )
        superseded_ref = (
            self._collection(account_id, "questions").document(supersedes_question_id)
            if supersedes_question_id is not None
            else None
        )
        account_ref = self._account(account_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def open_pattern(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            existing_snapshot = await question_ref.get(transaction=transaction)
            topic_snapshot = (
                await topic_ref.get(transaction=transaction) if topic_ref is not None else None
            )
            predecessor_ref = None
            predecessor_snapshot = None
            if topic_snapshot is not None and topic_snapshot.exists:
                predecessor_id = topic_snapshot.get("latest_question_id")
                predecessor_ref = self._collection(account_id, "questions").document(predecessor_id)
                predecessor_snapshot = await predecessor_ref.get(transaction=transaction)
            superseded_snapshot = (
                await superseded_ref.get(transaction=transaction)
                if superseded_ref is not None
                else None
            )
            if (
                not account_snapshot.exists
                or account_snapshot.get("id") != account_id
                or account_snapshot.get("status") != "active"
            ):
                raise AccountNotProvisioned
            if existing_snapshot.exists:
                existing = _model(existing_snapshot, ClarificationQuestion)
                if existing.account_id != account_id:
                    raise QuestionNotFound
                return existing
            predecessor = None
            if predecessor_snapshot is not None:
                if not predecessor_snapshot.exists:
                    raise QuestionNotFound
                predecessor = _model(predecessor_snapshot, ClarificationQuestion)
                if predecessor.account_id != account_id:
                    raise QuestionNotFound
                if predecessor.status == QuestionStatus.OPEN:
                    return predecessor
                if predecessor.response_kind != QuestionResponseKind.REJECT:
                    return predecessor
                prior_support_ids = {
                    item.evidence.id for item in predecessor.pattern_supporting_examples
                }
                new_support_ids = {item.evidence.id for item in supporting_examples or []}
                if (
                    len(new_support_ids - prior_support_ids) < PATTERN_RESURFACE_MINIMUM_NEW_SUPPORT
                    or predecessor.pattern_observation_ended_at is None
                    or observation_ended_at is None
                    or observation_ended_at <= predecessor.pattern_observation_ended_at
                ):
                    return predecessor
            superseded = None
            if superseded_snapshot is not None:
                if not superseded_snapshot.exists:
                    raise QuestionNotFound
                superseded = _model(superseded_snapshot, ClarificationQuestion)
                if (
                    superseded.account_id != account_id
                    or superseded.kind != QuestionKind.PATTERN_HYPOTHESIS
                ):
                    raise QuestionNotFound
                if superseded.status != QuestionStatus.OPEN:
                    raise QuestionSuperseded
            created_question = (
                question.model_copy(update={"predecessor_question_id": predecessor.id})
                if predecessor is not None
                else question
            )
            transaction.create(question_ref, _document(created_question))
            if superseded is not None:
                transaction.update(
                    superseded_ref,
                    {
                        "status": QuestionStatus.SUPERSEDED,
                        "superseded_by_question_id": created_question.id,
                        "superseded_at": created_question.created_at,
                        "updated_at": created_question.created_at,
                    },
                )
            if topic_ref is not None:
                transaction.set(
                    topic_ref,
                    {
                        "schema_version": 1,
                        "account_id": account_id,
                        "topic_key": topic_key,
                        "latest_question_id": created_question.id,
                        "updated_at": created_question.created_at,
                    },
                )
            return created_question

        return await open_pattern(transaction)

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
        query = self._collection(account.id, "meals")
        if status is not None:
            query = query.where(filter=FieldFilter("status", "==", status.value))
        meals = [_model(snapshot, MealEntry) async for snapshot in query.stream()]
        return sorted(
            meals,
            key=lambda item: (item.occurred_at or item.created_at, item.id),
            reverse=True,
        )

    async def meal_for_owner(self, owner_user_id: str, meal_id: str) -> MealEntry:
        account = await self.account_for_owner(owner_user_id)
        snapshot = await self._collection(account.id, "meals").document(meal_id).get()
        if not snapshot.exists:
            raise MealNotFound
        meal = _model(snapshot, MealEntry)
        if meal.account_id != account.id:
            raise CrossAccountAccess
        return meal

    async def list_meal_revisions(self, owner_user_id: str, meal_id: str) -> list[MealRevision]:
        meal = await self.meal_for_owner(owner_user_id, meal_id)
        query = (
            self._collection(meal.account_id, "meals")
            .document(meal.id)
            .collection("revisions")
            .order_by("number")
        )
        return [_model(snapshot, MealRevision) async for snapshot in query.stream()]

    async def record_knowledge_revision(
        self,
        *,
        account_id: str,
        topic_key: str,
        expected_revision_number: int | None,
        draft: KnowledgeRevisionDraft,
        idempotency_key: str,
    ) -> KnowledgeRevisionResult:
        normalized_topic = normalize_knowledge_topic(topic_key)
        page_id = knowledge_page_id(account_id, normalized_topic)
        revision_id = sha256(idempotency_key.encode()).hexdigest()
        request_hash = knowledge_revision_request_hash(
            topic_key=normalized_topic,
            expected_revision_number=expected_revision_number,
            draft=draft,
        )
        account_ref = self._account(account_id)
        page_ref = self._collection(account_id, "knowledge").document(page_id)
        revision_ref = page_ref.collection("revisions").document(revision_id)
        request_ref = self._collection(account_id, "knowledge_revision_requests").document(
            revision_id
        )

        @firestore.async_transactional
        async def record(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            request_snapshot = await request_ref.get(transaction=transaction)
            page_snapshot = await page_ref.get(transaction=transaction)
            if not account_snapshot.exists:
                raise AccountNotProvisioned

            if request_snapshot.exists:
                request_data = request_snapshot.to_dict() or {}
                if (
                    request_data.get("account_id") != account_id
                    or request_data.get("page_id") != page_id
                    or request_data.get("revision_id") != revision_id
                    or request_data.get("request_hash") != request_hash
                ):
                    raise IdempotencyConflict
                revision_snapshot = await revision_ref.get(transaction=transaction)
                if not page_snapshot.exists or not revision_snapshot.exists:
                    raise ValueError("knowledge revision idempotency record is incomplete")
                current_page = _model(page_snapshot, KnowledgePage)
                revision = _model(revision_snapshot, KnowledgeRevision)
                return KnowledgeRevisionResult(
                    page=materialize_knowledge_page(
                        topic_key=normalized_topic,
                        revision=revision,
                        created_at=current_page.created_at,
                    ),
                    revision=revision,
                )

            previous = None
            existing_page = None
            if page_snapshot.exists:
                existing_page = _model(page_snapshot, KnowledgePage)
                if existing_page.account_id != account_id:
                    raise KnowledgePageNotFound
                previous_snapshot = await (
                    page_ref.collection("revisions")
                    .document(existing_page.current_revision_id)
                    .get(transaction=transaction)
                )
                if not previous_snapshot.exists:
                    raise ValueError("current knowledge revision is missing")
                previous = _model(previous_snapshot, KnowledgeRevision)

            validate_knowledge_revision(
                previous=previous,
                expected_revision_number=expected_revision_number,
                draft=draft,
            )
            created_at = utc_now()
            revision = KnowledgeRevision(
                **draft.model_dump(),
                id=revision_id,
                account_id=account_id,
                page_id=page_id,
                number=1 if previous is None else previous.number + 1,
                base_revision_number=(previous.number if previous is not None else None),
                previous_revision_id=(previous.id if previous is not None else None),
                created_at=created_at,
            )
            page = materialize_knowledge_page(
                topic_key=normalized_topic,
                revision=revision,
                created_at=(existing_page.created_at if existing_page else created_at),
            )
            transaction.create(revision_ref, _document(revision))
            transaction.set(page_ref, _document(page))
            transaction.create(
                request_ref,
                {
                    "schema_version": 1,
                    "account_id": account_id,
                    "page_id": page_id,
                    "revision_id": revision_id,
                    "request_hash": request_hash,
                    "created_at": created_at,
                },
            )
            return KnowledgeRevisionResult(page=page, revision=revision)

        for attempt in range(KNOWLEDGE_OUTER_RETRY_ATTEMPTS):
            try:
                return await record(
                    self._client.transaction(max_attempts=KNOWLEDGE_TRANSACTION_MAX_ATTEMPTS)
                )
            except ValueError as error:
                if not str(error).startswith(TRANSACTION_COMMIT_FAILURE_PREFIX):
                    raise

                committed = await self.knowledge_revision_result_for_request(
                    account_id=account_id,
                    idempotency_key=idempotency_key,
                )
                if committed is not None:
                    return committed

                current_snapshot = await page_ref.get()
                if current_snapshot.exists:
                    current_page = _model(current_snapshot, KnowledgePage)
                    if current_page.current_revision_number != expected_revision_number:
                        raise KnowledgeRevisionConflict from None
                if attempt + 1 == KNOWLEDGE_OUTER_RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(_transaction_retry_delay(revision_id, attempt))

        raise AssertionError("Knowledge transaction retry loop did not return or raise")

    async def knowledge_revision_result_for_request(
        self,
        *,
        account_id: str,
        idempotency_key: str,
    ) -> KnowledgeRevisionResult | None:
        revision_id = sha256(idempotency_key.encode()).hexdigest()
        request_snapshot = (
            await self._collection(
                account_id,
                "knowledge_revision_requests",
            )
            .document(revision_id)
            .get()
        )
        if not request_snapshot.exists:
            account_snapshot = await self._account(account_id).get()
            if not account_snapshot.exists:
                raise AccountNotProvisioned
            return None
        request_data = request_snapshot.to_dict() or {}
        if request_data.get("account_id") != account_id:
            raise KnowledgePageNotFound
        page_id = request_data.get("page_id")
        stored_revision_id = request_data.get("revision_id")
        if not isinstance(page_id, str) or not isinstance(stored_revision_id, str):
            raise ValueError("knowledge revision idempotency record is incomplete")
        page_ref = self._collection(account_id, "knowledge").document(page_id)
        page_snapshot = await page_ref.get()
        revision_snapshot = (
            await page_ref.collection("revisions").document(stored_revision_id).get()
        )
        if not page_snapshot.exists or not revision_snapshot.exists:
            raise ValueError("knowledge revision idempotency record is incomplete")
        current_page = _model(page_snapshot, KnowledgePage)
        revision = _model(revision_snapshot, KnowledgeRevision)
        return KnowledgeRevisionResult(
            page=materialize_knowledge_page(
                topic_key=current_page.topic_key,
                revision=revision,
                created_at=current_page.created_at,
            ),
            revision=revision,
        )

    async def current_knowledge_revision(
        self,
        *,
        account_id: str,
        topic_key: str,
    ) -> KnowledgeRevisionResult | None:
        normalized_topic = normalize_knowledge_topic(topic_key)
        page_ref = self._collection(account_id, "knowledge").document(
            knowledge_page_id(account_id, normalized_topic)
        )
        page_snapshot = await page_ref.get()
        if not page_snapshot.exists:
            account_snapshot = await self._account(account_id).get()
            if not account_snapshot.exists:
                raise AccountNotProvisioned
            return None
        page = _model(page_snapshot, KnowledgePage)
        if page.account_id != account_id:
            raise KnowledgePageNotFound
        revision_snapshot = (
            await page_ref.collection("revisions").document(page.current_revision_id).get()
        )
        if not revision_snapshot.exists:
            raise ValueError("current knowledge revision is missing")
        return KnowledgeRevisionResult(
            page=page,
            revision=_model(revision_snapshot, KnowledgeRevision),
        )

    async def knowledge_page_index_for_account(
        self,
        *,
        account_id: str,
        limit: int = 50,
    ) -> list[KnowledgePage]:
        if not 1 <= limit <= 100:
            raise ValueError("knowledge page limit must be between 1 and 100")
        account_snapshot = await self._account(account_id).get()
        if not account_snapshot.exists or account_snapshot.get("status") != "active":
            raise AccountNotProvisioned
        pages: list[KnowledgePage] = []
        for lifecycle in KnowledgeLifecycle:
            if lifecycle == KnowledgeLifecycle.RETIRED:
                continue
            query = (
                self._collection(account_id, "knowledge")
                .where(filter=FieldFilter("lifecycle", "==", lifecycle.value))
                .order_by("updated_at", direction=firestore.Query.DESCENDING)
                .limit(limit)
            )
            pages.extend([_model(snapshot, KnowledgePage) async for snapshot in query.stream()])
        if any(page.account_id != account_id for page in pages):
            raise KnowledgePageNotFound
        return sorted(
            pages,
            key=lambda item: (item.updated_at, item.id),
            reverse=True,
        )[:limit]

    async def active_knowledge_revision_for_account(
        self,
        *,
        account_id: str,
        page_id: str,
    ) -> KnowledgeRevisionResult:
        account_snapshot = await self._account(account_id).get()
        if not account_snapshot.exists or account_snapshot.get("status") != "active":
            raise AccountNotProvisioned
        page_ref = self._collection(account_id, "knowledge").document(page_id)
        page_snapshot = await page_ref.get()
        if not page_snapshot.exists:
            raise KnowledgePageNotFound
        page = _model(page_snapshot, KnowledgePage)
        if page.account_id != account_id or page.lifecycle == KnowledgeLifecycle.RETIRED:
            raise KnowledgePageNotFound
        revision_snapshot = (
            await page_ref.collection("revisions").document(page.current_revision_id).get()
        )
        if not revision_snapshot.exists:
            raise ValueError("current knowledge revision is missing")
        revision = _model(revision_snapshot, KnowledgeRevision)
        if (
            revision.account_id != account_id
            or revision.page_id != page.id
            or revision.id != page.current_revision_id
        ):
            raise KnowledgePageNotFound
        return KnowledgeRevisionResult(page=page, revision=revision)

    async def knowledge_page_for_owner(
        self,
        owner_user_id: str,
        page_id: str,
    ) -> KnowledgePage:
        account = await self.account_for_owner(owner_user_id)
        snapshot = await self._collection(account.id, "knowledge").document(page_id).get()
        if not snapshot.exists:
            raise KnowledgePageNotFound
        page = _model(snapshot, KnowledgePage)
        if page.account_id != account.id:
            raise KnowledgePageNotFound
        return page

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
        pages = await self.knowledge_page_index_for_account(
            account_id=account.id,
            limit=limit,
        )
        if include_retired:
            retired_query = (
                self._collection(account.id, "knowledge")
                .where(
                    filter=FieldFilter(
                        "lifecycle",
                        "==",
                        KnowledgeLifecycle.RETIRED.value,
                    )
                )
                .order_by("updated_at", direction=firestore.Query.DESCENDING)
                .limit(limit)
            )
            pages.extend(
                [_model(snapshot, KnowledgePage) async for snapshot in retired_query.stream()]
            )
        if any(page.account_id != account.id for page in pages):
            raise KnowledgePageNotFound
        return sorted(
            pages,
            key=lambda item: (item.updated_at, item.id),
            reverse=True,
        )[:limit]

    async def list_knowledge_revisions(
        self,
        owner_user_id: str,
        page_id: str,
    ) -> list[KnowledgeRevision]:
        page = await self.knowledge_page_for_owner(owner_user_id, page_id)
        query = (
            self._collection(page.account_id, "knowledge")
            .document(page.id)
            .collection("revisions")
            .order_by("number")
        )
        return [_model(snapshot, KnowledgeRevision) async for snapshot in query.stream()]

    async def record_meal_feedback(
        self,
        *,
        owner_user_id: str,
        meal_id: str,
        request: MealFeedbackRequest,
        idempotency_key: str,
    ) -> MealFeedbackResult:
        account = await self.account_for_owner(owner_user_id)
        return await self._record_feedback(
            account_id=account.id,
            meal_id=meal_id,
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
        query = (
            self._collection(account.id, "feedback")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        feedback: list[MealFeedbackView] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict() or {}
            data.pop("schema_version", None)
            data.pop("idempotency_hash", None)
            item = MealFeedbackView.model_validate(data)
            if item.account_id != account.id:
                raise CrossAccountAccess
            feedback.append(item)
        return feedback

    async def _record_feedback(
        self,
        *,
        account_id: str,
        meal_id: str,
        request: MealFeedbackRequest,
        idempotency_key: str,
    ) -> MealFeedbackResult:
        feedback_id = sha256(idempotency_key.encode()).hexdigest()
        feedback_ref = self._collection(account_id, "feedback").document(feedback_id)
        meal_ref = self._collection(account_id, "meals").document(meal_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def record(transaction):
            existing = await feedback_ref.get(transaction=transaction)
            meal_snapshot = await meal_ref.get(transaction=transaction)
            if not meal_snapshot.exists:
                raise MealNotFound
            meal = _model(meal_snapshot, MealEntry)
            if existing.exists:
                feedback_data = existing.to_dict()
                feedback_data.pop("schema_version", None)
                feedback_data.pop("idempotency_hash", None)
                feedback_data["idempotency_key"] = idempotency_key
                feedback = MealFeedback.model_validate(feedback_data)
                if (
                    feedback.account_id != account_id
                    or feedback.meal_id != meal.id
                    or feedback.kind != request.kind
                    or feedback.actual_meal != request.actual_meal
                    or feedback.explanation != request.explanation
                    or feedback.correction != request.correction
                    or feedback.base_revision_number != request.base_revision_number
                    or feedback.learning_disposition != request.learning_disposition
                    or feedback.question_id is not None
                ):
                    raise IdempotencyConflict
                revision_snapshot = (
                    await meal_ref.collection("revisions")
                    .document(feedback.id)
                    .get(transaction=transaction)
                )
                if not revision_snapshot.exists:
                    raise ValueError("Feedback revision is missing")
                result = MealFeedbackResult(
                    feedback=feedback,
                    revision=_model(revision_snapshot, MealRevision),
                )
            else:
                feedback = MealFeedback(
                    id=feedback_id,
                    account_id=account_id,
                    meal_id=meal.id,
                    kind=request.kind,
                    actual_meal=request.actual_meal,
                    explanation=request.explanation,
                    correction=request.correction,
                    base_revision_number=request.base_revision_number,
                    learning_disposition=request.learning_disposition,
                    idempotency_key=idempotency_key,
                )
                inference, status = revised_inference(meal, request)
                revision = MealRevision(
                    id=feedback.id,
                    account_id=account_id,
                    meal_id=meal.id,
                    number=meal.revision_number + 1,
                    status=status,
                    inference=inference,
                    activity_hypothesis=meal.activity_hypothesis,
                    source=MealRevisionSource.USER_FEEDBACK,
                    feedback_id=feedback.id,
                    base_revision_number=request.base_revision_number,
                    correction=request.correction,
                )
                updated = MealEntry(
                    **inference.model_dump(),
                    id=meal.id,
                    account_id=meal.account_id,
                    capture_id=meal.capture_id,
                    event_id=meal.event_id,
                    occurred_at=meal.occurred_at,
                    occurred_utc_offset_minutes=meal.occurred_utc_offset_minutes,
                    activity_hypothesis=meal.activity_hypothesis,
                    status=status,
                    revision_number=revision.number,
                    created_at=meal.created_at,
                )
                questions_to_supersede: list[Any] = []
                if request.kind == MealFeedbackKind.NOT_COOKING:
                    question_query = self._collection(account_id, "questions").where(
                        filter=FieldFilter("meal_id", "==", meal.id)
                    )
                    question_snapshots = await question_query.get(transaction=transaction)
                    for question_snapshot in question_snapshots:
                        question = _model(question_snapshot, ClarificationQuestion)
                        if question.account_id != account_id or question.meal_id != meal.id:
                            raise InvalidMealFeedbackTransition
                        if question.status == QuestionStatus.OPEN:
                            questions_to_supersede.append(question_snapshot.reference)
                feedback_data = _document(feedback, exclude={"idempotency_key"})
                feedback_data["idempotency_hash"] = feedback_id
                transaction.create(feedback_ref, feedback_data)
                transaction.create(
                    meal_ref.collection("revisions").document(revision.id), _document(revision)
                )
                transaction.set(meal_ref, _document(updated))
                for question_ref in questions_to_supersede:
                    transaction.update(
                        question_ref,
                        {
                            "status": QuestionStatus.SUPERSEDED,
                            "superseded_by_question_id": None,
                            "superseded_at": feedback.created_at,
                            "updated_at": feedback.created_at,
                        },
                    )
                result = MealFeedbackResult(feedback=feedback, revision=revision)
            return result

        return await record(transaction)

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
        note = UserContextNote(
            id=note_id,
            account_id=account.id,
            author_user_id=owner_user_id,
            **request.model_dump(mode="python"),
        )
        note_ref = self._collection(account.id, "user_context_notes").document(note.id)
        try:
            await note_ref.create(
                {
                    **_document(note),
                    "request_hash": request_hash,
                    "updated_at": note.created_at,
                }
            )
            return note
        except AlreadyExists:
            snapshot = await note_ref.get()
            if not snapshot.exists or snapshot.get("request_hash") != request_hash:
                raise IdempotencyConflict from None
            existing = _user_context_note_from_snapshot(snapshot)
            if existing.account_id != account.id or existing.author_user_id != owner_user_id:
                raise IdempotencyConflict from None
            return existing

    async def list_user_context_notes(
        self,
        owner_user_id: str,
        *,
        include_inactive: bool = False,
        active_at: datetime | None = None,
    ) -> list[UserContextNote]:
        account = await self.account_for_owner(owner_user_id)
        evaluated_at = active_at or utc_now()
        notes = [
            _user_context_note_from_snapshot(snapshot)
            async for snapshot in self._collection(account.id, "user_context_notes").stream()
        ]
        if any(note.account_id != account.id for note in notes):
            raise CrossAccountAccess
        visible = [note for note in notes if include_inactive or note.is_active_at(evaluated_at)]
        return sorted(
            visible,
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )

    async def retire_user_context_note(
        self,
        *,
        owner_user_id: str,
        note_id: str,
    ) -> UserContextNote:
        account = await self.account_for_owner(owner_user_id)
        note_ref = self._collection(account.id, "user_context_notes").document(note_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def retire(transaction):
            snapshot = await note_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise UserContextNoteNotFound
            note = _user_context_note_from_snapshot(snapshot)
            if note.account_id != account.id:
                raise UserContextNoteNotFound
            if note.status == UserContextNoteStatus.RETIRED:
                return note
            retired_at = utc_now()
            transaction.update(
                note_ref,
                {
                    "status": UserContextNoteStatus.RETIRED,
                    "retired_at": retired_at,
                    "updated_at": retired_at,
                },
            )
            return note.model_copy(
                update={
                    "status": UserContextNoteStatus.RETIRED,
                    "retired_at": retired_at,
                }
            )

        return await retire(transaction)

    async def recent_meals_for_account(
        self,
        *,
        account_id: str,
        limit: int = 20,
    ) -> list[MealEntry]:
        if not 1 <= limit <= 100:
            raise ValueError("recent meal limit must be between 1 and 100")
        account = await self._account(account_id).get()
        if not account.exists or account.get("status") != "active":
            raise AccountNotProvisioned
        meals = [
            _model(snapshot, MealEntry)
            async for snapshot in self._collection(account_id, "meals").stream()
        ]
        if any(meal.account_id != account_id for meal in meals):
            raise CrossAccountAccess
        return sorted(
            (
                meal
                for meal in meals
                if meal.event_id is not None and meal.status != MealStatus.NOT_COOKING
            ),
            key=lambda item: (item.occurred_at or item.created_at, item.id),
            reverse=True,
        )[:limit]

    async def recent_meal_evidence_for_account(
        self,
        *,
        account_id: str,
        limit: int = 20,
    ) -> list[tuple[MealEntry, MealRevision]]:
        meals = await self.recent_meals_for_account(account_id=account_id, limit=limit)
        result: list[tuple[MealEntry, MealRevision]] = []
        for meal in meals:
            snapshots = [
                snapshot
                async for snapshot in (
                    self._collection(account_id, "meals")
                    .document(meal.id)
                    .collection("revisions")
                    .where(filter=FieldFilter("number", "==", meal.revision_number))
                    .limit(2)
                    .stream()
                )
            ]
            if len(snapshots) != 1:
                raise MealRevisionConflict
            revision = _model(snapshots[0], MealRevision)
            if revision.account_id != account_id or revision.meal_id != meal.id:
                raise MealRevisionConflict
            result.append((meal, revision))
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
        account = await self._account(account_id).get()
        if not account.exists or account.get("status") != "active":
            raise AccountNotProvisioned
        evaluated_at = active_at or utc_now()
        notes = [
            _user_context_note_from_snapshot(snapshot)
            async for snapshot in self._collection(account_id, "user_context_notes").stream()
        ]
        if any(note.account_id != account_id for note in notes):
            raise CrossAccountAccess
        return sorted(
            (note for note in notes if note.is_active_at(evaluated_at)),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )[:limit]

    async def unresolved_reviews_for_account(
        self,
        *,
        account_id: str,
        limit: int = 20,
    ) -> tuple[list[MealEntry], list[ClarificationQuestion]]:
        if not 1 <= limit <= 100:
            raise ValueError("unresolved review limit must be between 1 and 100")
        account = await self._account(account_id).get()
        if not account.exists or account.get("status") != "active":
            raise AccountNotProvisioned
        meals = [
            _model(snapshot, MealEntry)
            async for snapshot in self._collection(account_id, "meals").stream()
        ]
        questions = [
            _model(snapshot, ClarificationQuestion)
            async for snapshot in self._collection(account_id, "questions").stream()
        ]
        if any(meal.account_id != account_id for meal in meals) or any(
            question.account_id != account_id for question in questions
        ):
            raise CrossAccountAccess
        unresolved_meals = sorted(
            (
                meal
                for meal in meals
                if meal.status in {MealStatus.PROVISIONAL, MealStatus.CONTRADICTED}
            ),
            key=lambda item: (item.occurred_at or item.created_at, item.id),
            reverse=True,
        )[:limit]
        open_questions = sorted(
            (question for question in questions if question.status == QuestionStatus.OPEN),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )[:limit]
        return unresolved_meals, open_questions

    async def list_questions(
        self,
        owner_user_id: str,
        *,
        question_status: QuestionStatus | None = None,
    ) -> list[ClarificationQuestion]:
        account = await self.account_for_owner(owner_user_id)
        query = self._collection(account.id, "questions")
        if question_status is not None:
            query = query.where(filter=FieldFilter("status", "==", question_status))
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING)
        return [_model(snapshot, ClarificationQuestion) async for snapshot in query.stream()]

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
        response_id = sha256(idempotency_key.encode()).hexdigest()
        question_ref = self._collection(account.id, "questions").document(question_id)
        response_ref = self._collection(account.id, "question_responses").document(response_id)

        async def record_once() -> QuestionResponseResult:
            transaction = self._client.transaction()

            @firestore.async_transactional
            async def record(transaction):
                question_snapshot = await question_ref.get(transaction=transaction)
                response_snapshot = await response_ref.get(transaction=transaction)
                if not question_snapshot.exists:
                    raise QuestionNotFound
                question = _model(question_snapshot, ClarificationQuestion)
                if question.account_id != account.id:
                    raise QuestionNotFound

                if response_snapshot.exists:
                    response_data = response_snapshot.to_dict() or {}
                    response_data.pop("schema_version", None)
                    response_data.pop("idempotency_hash", None)
                    response_data["idempotency_key"] = idempotency_key
                    response = QuestionResponse.model_validate(response_data)
                    if (
                        response.account_id != account.id
                        or response.question_id != question.id
                        or response.kind != request.kind
                        or response.correction != request.correction
                        or response.explanation != request.explanation
                    ):
                        raise IdempotencyConflict
                    feedback = None
                    revision = None
                    if response.feedback_id is not None:
                        if question.meal_id is None:
                            raise ValueError("event response is missing its meal")
                        feedback_snapshot = await (
                            self._collection(account.id, "feedback")
                            .document(response.feedback_id)
                            .get(transaction=transaction)
                        )
                        revision_snapshot = await (
                            self._collection(account.id, "meals")
                            .document(question.meal_id)
                            .collection("revisions")
                            .document(response.feedback_id)
                            .get(transaction=transaction)
                        )
                        if not feedback_snapshot.exists or not revision_snapshot.exists:
                            raise ValueError("question response derivation is incomplete")
                        feedback_data = feedback_snapshot.to_dict() or {}
                        feedback_data.pop("schema_version", None)
                        feedback_data.pop("idempotency_hash", None)
                        feedback_data["idempotency_key"] = idempotency_key
                        feedback = MealFeedback.model_validate(feedback_data)
                        revision = _model(revision_snapshot, MealRevision)
                    return QuestionResponseResult(
                        question=question,
                        response=response,
                        feedback=feedback,
                        revision=revision,
                    )

                if (
                    question.status == QuestionStatus.ANSWERED
                    and question.response_id is None
                    and question.kind == QuestionKind.EVENT_CLARIFICATION
                    and question.meal_id is not None
                ):
                    legacy_feedback_ref = self._collection(account.id, "feedback").document(
                        response_id
                    )
                    legacy_revision_ref = (
                        self._collection(account.id, "meals")
                        .document(question.meal_id)
                        .collection("revisions")
                        .document(response_id)
                    )
                    legacy_feedback_snapshot = await legacy_feedback_ref.get(
                        transaction=transaction
                    )
                    legacy_revision_snapshot = await legacy_revision_ref.get(
                        transaction=transaction
                    )
                    if legacy_feedback_snapshot.exists and legacy_revision_snapshot.exists:
                        feedback_data = legacy_feedback_snapshot.to_dict() or {}
                        feedback_data.pop("schema_version", None)
                        feedback_data.pop("idempotency_hash", None)
                        feedback_data["idempotency_key"] = idempotency_key
                        feedback = MealFeedback.model_validate(feedback_data)
                        if (
                            request.kind != QuestionResponseKind.CORRECT
                            or feedback.account_id != account.id
                            or feedback.question_id != question.id
                            or feedback.kind != MealFeedbackKind.CORRECT
                            or feedback.actual_meal != request.correction
                            or feedback.explanation != request.explanation
                        ):
                            raise IdempotencyConflict
                        revision = _model(legacy_revision_snapshot, MealRevision)
                        response = QuestionResponse(
                            id=response_id,
                            account_id=account.id,
                            question_id=question.id,
                            kind=request.kind,
                            correction=request.correction,
                            explanation=request.explanation,
                            idempotency_key=idempotency_key,
                            feedback_id=feedback.id,
                            created_at=feedback.created_at,
                        )
                        response_data = _document(
                            response,
                            exclude={"idempotency_key"},
                        )
                        response_data["idempotency_hash"] = response_id
                        transaction.create(response_ref, response_data)
                        transaction.update(
                            question_ref,
                            {
                                "response_kind": request.kind,
                                "response_id": response.id,
                                "updated_at": response.created_at,
                            },
                        )
                        upgraded_question = question.model_copy(
                            update={
                                "response_kind": request.kind,
                                "response_id": response.id,
                            }
                        )
                        return QuestionResponseResult(
                            question=upgraded_question,
                            response=response,
                            feedback=feedback,
                            revision=revision,
                        )

                if question.status == QuestionStatus.SUPERSEDED:
                    raise QuestionSuperseded
                if question.status != QuestionStatus.OPEN:
                    raise QuestionAlreadyAnswered

                feedback = None
                revision = None
                feedback_id = None
                if question.kind == QuestionKind.EVENT_CLARIFICATION and request.kind in {
                    QuestionResponseKind.CONFIRM,
                    QuestionResponseKind.CORRECT,
                }:
                    if question.meal_id is None:
                        raise ValueError("event question is missing its meal")
                    meal_ref = self._collection(account.id, "meals").document(question.meal_id)
                    feedback_ref = self._collection(account.id, "feedback").document(response_id)
                    meal_snapshot = await meal_ref.get(transaction=transaction)
                    feedback_snapshot = await feedback_ref.get(transaction=transaction)
                    if not meal_snapshot.exists:
                        raise MealNotFound
                    if feedback_snapshot.exists:
                        raise IdempotencyConflict
                    meal = _model(meal_snapshot, MealEntry)
                    if meal.account_id != account.id:
                        raise MealNotFound
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
                    feedback = MealFeedback(
                        id=response_id,
                        account_id=account.id,
                        meal_id=meal.id,
                        kind=feedback_request.kind,
                        actual_meal=feedback_request.actual_meal,
                        explanation=feedback_request.explanation,
                        correction=feedback_request.correction,
                        base_revision_number=feedback_request.base_revision_number,
                        idempotency_key=idempotency_key,
                        question_id=question.id,
                    )
                    inference, meal_status = revised_inference(meal, feedback_request)
                    revision = MealRevision(
                        id=feedback.id,
                        account_id=account.id,
                        meal_id=meal.id,
                        number=meal.revision_number + 1,
                        status=meal_status,
                        inference=inference,
                        activity_hypothesis=meal.activity_hypothesis,
                        source=MealRevisionSource.USER_FEEDBACK,
                        feedback_id=feedback.id,
                        base_revision_number=feedback_request.base_revision_number,
                        correction=feedback_request.correction,
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
                    feedback_data = _document(feedback, exclude={"idempotency_key"})
                    feedback_data["idempotency_hash"] = response_id
                    transaction.create(feedback_ref, feedback_data)
                    transaction.create(
                        meal_ref.collection("revisions").document(revision.id),
                        _document(revision),
                    )
                    transaction.set(meal_ref, _document(updated_meal))
                    feedback_id = feedback.id

                response = QuestionResponse(
                    id=response_id,
                    account_id=account.id,
                    question_id=question.id,
                    kind=request.kind,
                    correction=request.correction,
                    explanation=request.explanation,
                    idempotency_key=idempotency_key,
                    feedback_id=feedback_id,
                )
                response_data = _document(response, exclude={"idempotency_key"})
                response_data["idempotency_hash"] = response_id
                transaction.create(response_ref, response_data)
                transaction.update(
                    question_ref,
                    {
                        "status": QuestionStatus.ANSWERED,
                        "answer": request.correction or request.kind.value,
                        "learning_tip": request.explanation,
                        "response_kind": request.kind,
                        "response_id": response.id,
                        "answered_at": response.created_at,
                        "updated_at": response.created_at,
                    },
                )
                answered_question = question.model_copy(
                    update={
                        "status": QuestionStatus.ANSWERED,
                        "answer": request.correction or request.kind.value,
                        "learning_tip": request.explanation,
                        "response_kind": request.kind,
                        "response_id": response.id,
                        "answered_at": response.created_at,
                    }
                )
                return QuestionResponseResult(
                    question=answered_question,
                    response=response,
                    feedback=feedback,
                    revision=revision,
                )

            return await record(transaction)

        retry_jitter = int(response_id[:4], 16) / 65_535 * 0.01
        for fresh_attempt in range(5):
            try:
                return await record_once()
            except ValueError as error:
                if (
                    str(error) != "Failed to commit transaction in 5 attempts."
                    or fresh_attempt == 4
                ):
                    raise
                # Firestore already retries one transaction internally. A pair of
                # simultaneous responses can still exhaust those attempts in lockstep,
                # especially in the emulator. Fresh transactions use bounded,
                # identity-jittered backoff so one observes the committed winner.
                await asyncio.sleep((0.01 * (2**fresh_attempt)) + retry_jitter)
        raise AssertionError("unreachable transaction retry state")

    async def list_question_responses_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 200,
    ) -> list[QuestionResponseView]:
        if not 1 <= limit <= 200:
            raise ValueError("question response list limit must be between 1 and 200")
        account = await self.account_for_owner(owner_user_id)
        query = (
            self._collection(account.id, "question_responses")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        responses: list[QuestionResponseView] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict() or {}
            data.pop("schema_version", None)
            data.pop("idempotency_hash", None)
            item = QuestionResponseView.model_validate(data)
            if item.account_id != account.id:
                raise CrossAccountAccess
            responses.append(item)
        return responses

    async def capture_for_owner(self, owner_user_id: str, capture_id: str) -> CaptureRecord:
        account = await self.account_for_owner(owner_user_id)
        snapshot = await self._collection(account.id, "captures").document(capture_id).get()
        if not snapshot.exists:
            raise CaptureNotFound
        capture = self._capture_from_snapshot(snapshot, "")
        if capture.account_id != account.id:
            raise CrossAccountAccess
        return capture

    async def recent_captures_for_owner(
        self,
        owner_user_id: str,
        *,
        limit: int = 20,
    ) -> list[CaptureRecord]:
        if not 1 <= limit <= 200:
            raise ValueError("capture list limit must be between 1 and 200")
        account = await self.account_for_owner(owner_user_id)
        query = (
            self._collection(account.id, "captures")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [self._capture_from_snapshot(snapshot, "") async for snapshot in query.stream()]

    @staticmethod
    def _capture_from_snapshot(snapshot, idempotency_key: str) -> CaptureRecord:
        data = snapshot.to_dict()
        data.pop("schema_version", None)
        data.pop("idempotency_hash", None)
        data.pop("received_at", None)
        data.pop("updated_at", None)
        data["idempotency_key"] = idempotency_key
        return CaptureRecord.model_validate(data)

    @staticmethod
    def _account_with_entitlement(account: Account, entitlement) -> Account:
        entitlement_data = entitlement.to_dict() or {}
        return account.model_copy(
            update={
                "trial_image_limit": entitlement_data.get("trial_image_limit"),
                "entitlement_mode": EntitlementMode(
                    entitlement_data.get("entitlement_mode") or EntitlementMode.TRIAL
                ),
                "accepted_image_count": entitlement_data.get("accepted_image_count"),
            }
        )
