from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from google.cloud import firestore
from google.cloud.firestore_v1 import DocumentSnapshot
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from .errors import (
    AccountAlreadyProvisioned,
    AccountCapacityReached,
    AccountNotProvisioned,
    CameraNotFound,
    CaptureNotFound,
    CrossAccountAccess,
    DeviceCredentialCollision,
    IdempotencyConflict,
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
    DeviceCamera,
    DeviceCredentialRecord,
    DeviceCredentialStatus,
    DurableJob,
    EntitlementMode,
    JobKind,
    JobStatus,
    LaunchMailConsent,
    MealEntry,
    MealFeedback,
    MealFeedbackKind,
    MealFeedbackRequest,
    MealFeedbackResult,
    MealRevision,
    MealRevisionSource,
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
from .repository import (
    inference_from_meal,
    revised_inference,
    validate_capture_scope,
    validate_enqueueable_job,
)

ENTITLEMENT_MODE_VALUES = frozenset(item.value for item in EntitlementMode)


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
        client: AsyncClient | None = None,
    ) -> None:
        self._client = client or AsyncClient(project=project_id)
        self._public_account_limit = public_account_limit
        self._trial_image_limit = trial_image_limit
        self._unlimited_owner_user_ids = frozenset(unlimited_owner_user_ids or set())

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
        transaction = self._client.transaction()

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
                count = capacity.get("active_account_count") if capacity.exists else 0
                stored_limit = (
                    capacity.get("account_limit") if capacity.exists else self._public_account_limit
                )
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise ValueError("Public account capacity count is invalid")
                if (
                    not isinstance(stored_limit, int)
                    or isinstance(stored_limit, bool)
                    or stored_limit < 1
                ):
                    raise ValueError("Public account capacity limit is invalid")
                limit = min(stored_limit, self._public_account_limit)
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

        return await provision(transaction)

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
        consent = LaunchMailConsent(
            id=consent_id,
            account_id=account.id,
            actor_user_id=owner_user_id,
            email_normalized=email_normalized,
            granted=granted,
            policy_version=policy_version,
        )
        identity_ref = self._identity(owner_user_id)
        consent_ref = self._collection(account.id, "consents").document(consent.id)
        transaction = self._client.transaction()

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
                return _model(existing, LaunchMailConsent)
            transaction.create(consent_ref, _document(consent))
            transaction.update(
                identity_ref,
                {
                    "email_normalized": email_normalized,
                    "mailing_list_opt_in": granted,
                    "mailing_list_policy_version": policy_version,
                    "updated_at": consent.created_at,
                },
            )
            return consent

        return await record(transaction)

    async def join_waitlist(
        self,
        *,
        firebase_uid: str,
        email_normalized: str,
        policy_version: str,
    ) -> WaitlistEntry:
        entry_id = sha256(email_normalized.encode()).hexdigest()
        entry = WaitlistEntry(
            id=entry_id,
            firebase_uid=firebase_uid,
            email_normalized=email_normalized,
            policy_version=policy_version,
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
                if stored.firebase_uid != firebase_uid:
                    raise CrossAccountAccess
                return stored
            count = capacity.get("active_account_count") if capacity.exists else 0
            stored_limit = (
                capacity.get("account_limit") if capacity.exists else self._public_account_limit
            )
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("Public account capacity count is invalid")
            if (
                not isinstance(stored_limit, int)
                or isinstance(stored_limit, bool)
                or stored_limit < 1
            ):
                raise ValueError("Public account capacity limit is invalid")
            if count < min(stored_limit, self._public_account_limit):
                raise WaitlistUnavailable
            transaction.create(waitlist_ref, _document(entry))
            return entry

        return await join(transaction)

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
            key_hash = snapshot.get("idempotency_hash")
            now = utc_now()
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
        meal_snapshot = await self._collection(account_id, "meals").document(meal.id).get()
        if not meal_snapshot.exists or meal_snapshot.get("account_id") != account_id:
            raise MealNotFound
        stored_meal = _model(meal_snapshot, MealEntry)
        reference = self._collection(account_id, "questions").document(stored_meal.id)
        existing = await reference.get()
        if existing.exists:
            question = _model(existing, ClarificationQuestion)
            if question.account_id != account_id:
                raise QuestionNotFound
            return question
        question = ClarificationQuestion(
            id=stored_meal.id,
            account_id=account_id,
            meal_id=stored_meal.id,
            prompt=prompt,
            reason=reason,
        )
        await reference.create(_document(question))
        return question

    async def list_meals(self, owner_user_id: str) -> list[MealEntry]:
        account = await self.account_for_owner(owner_user_id)
        query = self._collection(account.id, "meals").order_by(
            "created_at", direction=firestore.Query.DESCENDING
        )
        return [_model(snapshot, MealEntry) async for snapshot in query.stream()]

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

    async def _record_feedback(
        self,
        *,
        account_id: str,
        meal_id: str,
        request: MealFeedbackRequest,
        idempotency_key: str,
        question_id: str | None = None,
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
                    feedback.meal_id != meal.id
                    or feedback.kind != request.kind
                    or feedback.actual_meal != request.actual_meal
                    or feedback.explanation != request.explanation
                    or feedback.question_id != question_id
                ):
                    raise IdempotencyConflict
                revision_snapshot = (
                    await meal_ref.collection("revisions")
                    .document(feedback.id)
                    .get(transaction=transaction)
                )
                return MealFeedbackResult(
                    feedback=feedback,
                    revision=_model(revision_snapshot, MealRevision),
                )

            feedback = MealFeedback(
                id=feedback_id,
                account_id=account_id,
                meal_id=meal.id,
                kind=request.kind,
                actual_meal=request.actual_meal,
                explanation=request.explanation,
                idempotency_key=idempotency_key,
                question_id=question_id,
            )
            inference, status = revised_inference(meal, request)
            revision = MealRevision(
                id=feedback.id,
                account_id=account_id,
                meal_id=meal.id,
                number=meal.revision_number + 1,
                status=status,
                inference=inference,
                source=MealRevisionSource.USER_FEEDBACK,
                feedback_id=feedback.id,
            )
            updated = MealEntry(
                **inference.model_dump(),
                id=meal.id,
                account_id=meal.account_id,
                capture_id=meal.capture_id,
                status=status,
                revision_number=revision.number,
                created_at=meal.created_at,
            )
            feedback_data = _document(feedback, exclude={"idempotency_key"})
            feedback_data["idempotency_hash"] = feedback_id
            transaction.create(feedback_ref, feedback_data)
            transaction.create(
                meal_ref.collection("revisions").document(revision.id), _document(revision)
            )
            transaction.set(meal_ref, _document(updated))
            return MealFeedbackResult(feedback=feedback, revision=revision)

        return await record(transaction)

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
        account = await self.account_for_owner(owner_user_id)
        question_ref = self._collection(account.id, "questions").document(question_id)
        question_snapshot = await question_ref.get()
        if not question_snapshot.exists:
            raise QuestionNotFound
        question = _model(question_snapshot, ClarificationQuestion)
        if question.status == QuestionStatus.ANSWERED:
            feedback_ref = self._collection(account.id, "feedback").document(
                sha256(idempotency_key.encode()).hexdigest()
            )
            if not (await feedback_ref.get()).exists:
                raise QuestionAlreadyAnswered
        result = await self._record_feedback(
            account_id=account.id,
            meal_id=question.meal_id,
            request=MealFeedbackRequest(
                kind=MealFeedbackKind.CORRECT,
                actual_meal=request.answer,
                explanation=request.learning_tip,
            ),
            idempotency_key=idempotency_key,
            question_id=question.id,
        )
        if question.status == QuestionStatus.OPEN:
            answered_at = utc_now()
            await question_ref.update(
                {
                    "status": QuestionStatus.ANSWERED,
                    "answer": request.answer,
                    "learning_tip": request.learning_tip,
                    "answered_at": answered_at,
                    "updated_at": answered_at,
                }
            )
            question = question.model_copy(
                update={
                    "status": QuestionStatus.ANSWERED,
                    "answer": request.answer,
                    "learning_tip": request.learning_tip,
                    "answered_at": answered_at,
                }
            )
        return QuestionAnswerResult(
            question=question,
            feedback=result.feedback,
            revision=result.revision,
        )

    async def capture_for_owner(self, owner_user_id: str, capture_id: str) -> CaptureRecord:
        account = await self.account_for_owner(owner_user_id)
        snapshot = await self._collection(account.id, "captures").document(capture_id).get()
        if not snapshot.exists:
            raise CaptureNotFound
        capture = self._capture_from_snapshot(snapshot, "")
        if capture.account_id != account.id:
            raise CrossAccountAccess
        return capture

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
