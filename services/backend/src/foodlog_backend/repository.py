import asyncio
from collections.abc import Iterable
from datetime import datetime
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from .errors import (
    AccountAlreadyProvisioned,
    AccountCapacityReached,
    AccountNotProvisioned,
    CameraNotFound,
    CaptureNotFound,
    CrossAccountAccess,
    DeviceCredentialCollision,
    IdempotencyConflict,
    InboundAddressCollision,
    InboundAddressStateConflict,
    InvalidDeviceCredential,
    JobIdentityConflict,
    MealNotFound,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    TrialQuotaExhausted,
    WaitlistUnavailable,
)
from .grouping import (
    ACCOUNT_EVENT_HEAD_ID,
    CaptureGroupingResult,
    GroupingPolicy,
    capture_activity_time,
    segment_identity,
)
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
    Confidence,
    DeviceCamera,
    DeviceCredentialRecord,
    DeviceCredentialStatus,
    DurableJob,
    EntitlementMode,
    InboundMailAddress,
    InboundMailRoute,
    JobKind,
    JobStatus,
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
    NotificationOutboxStatus,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionStatus,
    VerifiedDeviceIdentity,
    WaitlistEntry,
    capture_grouping_job_id,
    event_inference_job_id,
    utc_now,
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

    async def save_meal(self, *, account_id: str, meal: MealEntry) -> MealEntry: ...

    async def open_question(
        self,
        *,
        account_id: str,
        meal: MealEntry,
        prompt: str,
        reason: str,
    ) -> ClarificationQuestion: ...

    async def list_meals(self, owner_user_id: str) -> list[MealEntry]: ...

    async def meal_for_owner(self, owner_user_id: str, meal_id: str) -> MealEntry: ...

    async def list_meal_revisions(self, owner_user_id: str, meal_id: str) -> list[MealRevision]: ...

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

    async def capture_for_owner(self, owner_user_id: str, capture_id: str) -> CaptureRecord: ...


class InMemoryRepository:
    """Concurrency-safe local adapter mirroring the required Firestore invariants."""

    def __init__(
        self,
        *,
        public_account_limit: int,
        trial_image_limit: int,
        unlimited_owner_user_ids: set[str] | None = None,
    ) -> None:
        self._public_account_limit = public_account_limit
        self._trial_image_limit = trial_image_limit
        self._unlimited_owner_user_ids = frozenset(unlimited_owner_user_ids or set())
        self._accounts: dict[str, Account] = {}
        self._account_by_owner: dict[str, str] = {}
        self._notification_outbox: dict[str, AccountCreatedOutbox] = {}
        self._launch_consents: dict[str, LaunchMailConsent] = {}
        self._waitlist_by_email_hash: dict[str, WaitlistEntry] = {}
        self._inbound_mail_addresses: dict[str, InboundMailAddress] = {}
        self._inbound_mail_routes: dict[str, InboundMailRoute] = {}
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
        self._feedback: dict[str, MealFeedback] = {}
        self._feedback_by_idempotency: dict[tuple[str, str], str] = {}
        self._revision_by_feedback: dict[str, MealRevision] = {}
        self._questions: dict[str, ClarificationQuestion] = {}
        self._question_by_meal: dict[str, str] = {}
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
            question = ClarificationQuestion(
                id=str(uuid4()),
                account_id=account_id,
                meal_id=stored_meal.id,
                prompt=prompt,
                reason=reason,
            )
            self._questions[question.id] = question
            self._question_by_meal[meal.id] = question.id
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
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            question = self._questions.get(question_id)
            if not question:
                raise QuestionNotFound
            if question.account_id != account.id:
                raise CrossAccountAccess

            feedback_request = MealFeedbackRequest(
                kind=MealFeedbackKind.CORRECT,
                actual_meal=request.answer,
                explanation=request.learning_tip,
            )
            duplicate_id = self._feedback_by_idempotency.get((account.id, idempotency_key))
            if question.status == QuestionStatus.ANSWERED and duplicate_id is None:
                raise QuestionAlreadyAnswered

            meal = self._owned_meal(account.id, question.meal_id)
            result = self._record_feedback_locked(
                account_id=account.id,
                meal=meal,
                request=feedback_request,
                idempotency_key=idempotency_key,
                question_id=question.id,
            )
            if question.status == QuestionStatus.OPEN:
                question.status = QuestionStatus.ANSWERED
                question.answer = request.answer
                question.learning_tip = request.learning_tip
                question.answered_at = utc_now()
            return QuestionAnswerResult(
                question=question.model_copy(deep=True),
                feedback=result.feedback,
                revision=result.revision,
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
            source=MealRevisionSource.USER_FEEDBACK,
            feedback_id=feedback.id,
        )
        updated_meal = MealEntry(
            **inference.model_dump(),
            id=meal.id,
            account_id=meal.account_id,
            capture_id=meal.capture_id,
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
