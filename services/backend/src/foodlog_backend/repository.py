import asyncio
from collections.abc import Iterable
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
    InvalidDeviceCredential,
    MealNotFound,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    TrialQuotaExhausted,
    WaitlistUnavailable,
)
from .models import (
    Account,
    BrowserCamera,
    CaptureEnvelopeV1,
    CaptureRecord,
    CaptureStatus,
    ClarificationQuestion,
    Confidence,
    DeviceCamera,
    DeviceCameraStatus,
    DeviceCredentialRecord,
    DeviceCredentialStatus,
    EntitlementMode,
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
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionStatus,
    VerifiedDeviceIdentity,
    WaitlistEntry,
    utc_now,
)


class Repository(Protocol):
    async def provision_account(self, owner_user_id: str) -> Account: ...

    async def account_for_owner(self, owner_user_id: str) -> Account: ...

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

    async def device_camera_for_identity(
        self,
        *,
        account_id: str,
        camera_id: str,
    ) -> DeviceCamera: ...

    async def create_browser_camera(self, owner_user_id: str, name: str) -> BrowserCamera: ...

    async def camera_for_owner(self, owner_user_id: str, camera_id: str) -> BrowserCamera: ...

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

    async def cancel_capture(self, capture: CaptureRecord) -> None: ...

    async def mark_stored(self, capture_id: str, account_id: str | None = None) -> None: ...

    async def mark_processed(self, capture_id: str, account_id: str | None = None) -> None: ...

    async def save_meal(self, meal: MealEntry) -> MealEntry: ...

    async def open_question(
        self, *, meal: MealEntry, prompt: str, reason: str
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
        self._launch_consents: dict[str, LaunchMailConsent] = {}
        self._waitlist_by_email_hash: dict[str, WaitlistEntry] = {}
        self._device_cameras: dict[str, DeviceCamera] = {}
        self._device_credentials: dict[str, DeviceCredentialRecord] = {}
        self._cameras: dict[str, BrowserCamera] = {}
        self._browser_camera_by_account: dict[str, str] = {}
        self._captures: dict[str, CaptureRecord] = {}
        self._capture_by_idempotency: dict[tuple[str, str], str] = {}
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
                    self._trial_image_limit
                    if entitlement_mode == EntitlementMode.TRIAL
                    else None
                ),
            )
            self._accounts[account.id] = account
            self._account_by_owner[owner_user_id] = account.id
            return account.model_copy(deep=True)

    async def account_for_owner(self, owner_user_id: str) -> Account:
        async with self._lock:
            account_id = self._account_by_owner.get(owner_user_id)
            if not account_id:
                raise AccountNotProvisioned
            return self._accounts[account_id].model_copy(deep=True)

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
                or (
                    credential.expires_at is not None
                    and credential.expires_at <= now
                )
            ):
                raise InvalidDeviceCredential
            camera = self._device_cameras.get(credential.camera_id)
            account = self._accounts.get(credential.account_id)
            if (
                camera is None
                or camera.account_id != credential.account_id
                or camera.status != DeviceCameraStatus.ACTIVE
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
        account = await self.account_for_owner(owner_user_id)
        now = utc_now()
        async with self._lock:
            camera = self._device_cameras.get(camera_id)
            if camera is None or camera.account_id != account.id:
                raise CameraNotFound
            if camera.status == DeviceCameraStatus.REVOKED:
                return camera.model_copy(deep=True)
            revoked_camera = camera.model_copy(
                update={
                    "status": DeviceCameraStatus.REVOKED,
                    "revoked_at": now,
                }
            )
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
                or camera.status != DeviceCameraStatus.ACTIVE
            ):
                raise CameraNotFound
            return camera.model_copy(deep=True)

    async def create_browser_camera(self, owner_user_id: str, name: str) -> BrowserCamera:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            existing_id = self._browser_camera_by_account.get(account.id)
            if existing_id:
                return self._cameras[existing_id].model_copy(deep=True)
            camera = BrowserCamera(id=str(uuid4()), account_id=account.id, name=name)
            self._cameras[camera.id] = camera
            self._browser_camera_by_account[account.id] = camera.id
            return camera.model_copy(deep=True)

    async def camera_for_owner(self, owner_user_id: str, camera_id: str) -> BrowserCamera:
        account = await self.account_for_owner(owner_user_id)
        async with self._lock:
            camera = self._cameras.get(camera_id)
            if not camera:
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
        async with self._lock:
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
            stored_account = self._accounts[account.id]
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

    async def cancel_capture(self, capture: CaptureRecord) -> None:
        async with self._lock:
            stored = self._captures.pop(capture.id, None)
            if not stored:
                return
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

    async def mark_stored(self, capture_id: str, account_id: str | None = None) -> None:
        async with self._lock:
            capture = self._captures.get(capture_id)
            if not capture:
                raise CaptureNotFound
            if capture.status == CaptureStatus.ACCEPTED:
                capture.status = CaptureStatus.STORED

    async def mark_processed(self, capture_id: str, account_id: str | None = None) -> None:
        async with self._lock:
            capture = self._captures.get(capture_id)
            if not capture:
                raise CaptureNotFound
            capture.status = CaptureStatus.PROCESSED

    async def save_meal(self, meal: MealEntry) -> MealEntry:
        async with self._lock:
            existing_id = self._meal_by_capture.get(meal.capture_id)
            if existing_id:
                return self._meals[existing_id].model_copy(deep=True)
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
        meal: MealEntry,
        prompt: str,
        reason: str,
    ) -> ClarificationQuestion:
        async with self._lock:
            existing_id = self._question_by_meal.get(meal.id)
            if existing_id:
                return self._questions[existing_id].model_copy(deep=True)
            question = ClarificationQuestion(
                id=str(uuid4()),
                account_id=meal.account_id,
                meal_id=meal.id,
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
