from hashlib import sha256
from typing import Any
from uuid import uuid4

from google.cloud import firestore
from google.cloud.firestore_v1 import DocumentSnapshot
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from .errors import (
    AccountCapacityReached,
    AccountNotProvisioned,
    CameraNotFound,
    CaptureNotFound,
    CrossAccountAccess,
    IdempotencyConflict,
    MealNotFound,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    TrialQuotaExhausted,
)
from .models import (
    Account,
    BrowserCamera,
    CaptureRecord,
    CaptureStatus,
    ClarificationQuestion,
    MealEntry,
    MealFeedback,
    MealFeedbackKind,
    MealFeedbackRequest,
    MealFeedbackResult,
    MealRevision,
    MealRevisionSource,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionStatus,
    utc_now,
)
from .repository import inference_from_meal, revised_inference


def _document(model: BaseModel, *, exclude: set[str] | None = None) -> dict[str, Any]:
    data = model.model_dump(mode="python", exclude=exclude or set())
    data["schema_version"] = 1
    return data


def _model[ModelT: BaseModel](snapshot: DocumentSnapshot, model_type: type[ModelT]) -> ModelT:
    data = snapshot.to_dict()
    if data is None:
        raise ValueError(f"Document {snapshot.reference.path} has no data")
    data.pop("schema_version", None)
    data.pop("updated_at", None)
    return model_type.model_validate(data)


class FirestoreRepository:
    """Account-scoped production repository backed by Firestore Native mode."""

    def __init__(
        self,
        *,
        project_id: str,
        public_account_limit: int,
        trial_image_limit: int,
        client: AsyncClient | None = None,
    ) -> None:
        self._client = client or AsyncClient(project=project_id)
        self._public_account_limit = public_account_limit
        self._trial_image_limit = trial_image_limit

    def _identity(self, owner_user_id: str):
        return self._client.collection("identities").document(owner_user_id)

    def _account(self, account_id: str):
        return self._client.collection("accounts").document(account_id)

    def _entitlement(self, account_id: str):
        return self._account(account_id).collection("entitlements").document("current")

    def _collection(self, account_id: str, name: str):
        return self._account(account_id).collection(name)

    async def provision_account(self, owner_user_id: str) -> Account:
        account_id = str(uuid4())
        created_at = utc_now()
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

            capacity = await capacity_ref.get(transaction=transaction)
            count = capacity.get("active_account_count") if capacity.exists else 0
            limit = capacity.get("account_limit") if capacity.exists else self._public_account_limit
            if count >= limit:
                raise AccountCapacityReached

            account = Account(
                id=account_id,
                owner_user_id=owner_user_id,
                trial_image_limit=self._trial_image_limit,
                created_at=created_at,
            )
            transaction.create(
                self._account(account_id),
                {
                    "schema_version": 1,
                    "id": account_id,
                    "owner_user_id": owner_user_id,
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
                    "trial_image_limit": self._trial_image_limit,
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
            transaction.create(
                identity_ref,
                {
                    "schema_version": 1,
                    "account_id": account_id,
                    "status": "active",
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
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
        return Account(
            id=account.id,
            owner_user_id=account.get("owner_user_id"),
            trial_image_limit=entitlement.get("trial_image_limit"),
            accepted_image_count=entitlement.get("accepted_image_count"),
            created_at=account.get("created_at"),
        )

    async def create_browser_camera(self, owner_user_id: str, name: str) -> BrowserCamera:
        account = await self.account_for_owner(owner_user_id)
        account_ref = self._account(account.id)
        camera_id = str(uuid4())
        created_at = utc_now()
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def create(transaction):
            account_snapshot = await account_ref.get(transaction=transaction)
            existing_id = (account_snapshot.to_dict() or {}).get("primary_browser_camera_id")
            if existing_id:
                existing = (
                    await self._collection(account.id, "cameras")
                    .document(existing_id)
                    .get(transaction=transaction)
                )
                if existing.exists:
                    return _model(existing, BrowserCamera)
            camera = BrowserCamera(
                id=camera_id,
                account_id=account.id,
                name=name,
                created_at=created_at,
            )
            camera_ref = self._collection(account.id, "cameras").document(camera.id)
            transaction.create(camera_ref, _document(camera))
            transaction.update(
                account_ref,
                {"primary_browser_camera_id": camera.id, "updated_at": created_at},
            )
            return camera

        return await create(transaction)

    async def camera_for_owner(self, owner_user_id: str, camera_id: str) -> BrowserCamera:
        account = await self.account_for_owner(owner_user_id)
        snapshot = await self._collection(account.id, "cameras").document(camera_id).get()
        if not snapshot.exists:
            raise CameraNotFound
        camera = _model(snapshot, BrowserCamera)
        if camera.account_id != account.id:
            raise CrossAccountAccess
        return camera

    async def reserve_capture(
        self,
        *,
        capture_id: str,
        account: Account,
        camera: BrowserCamera,
        idempotency_key: str,
        content_type: str,
        content_sha256: str,
        object_key: str,
    ) -> tuple[CaptureRecord, Account, bool]:
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
            created_at=created_at,
        )
        capture_ref = self._collection(account.id, "captures").document(capture.id)
        idempotency_ref = self._collection(account.id, "capture_idempotency").document(key_hash)
        entitlement_ref = self._entitlement(account.id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def reserve(transaction):
            duplicate = await idempotency_ref.get(transaction=transaction)
            entitlement = await entitlement_ref.get(transaction=transaction)
            if not entitlement.exists:
                raise AccountNotProvisioned
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
                ):
                    raise IdempotencyConflict
                return record, self._account_with_entitlement(account, entitlement), False

            count = entitlement.get("accepted_image_count")
            limit = entitlement.get("trial_image_limit")
            if count >= limit:
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

    async def cancel_capture(self, capture: CaptureRecord) -> None:
        capture_ref = self._collection(capture.account_id, "captures").document(capture.id)
        entitlement_ref = self._entitlement(capture.account_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def cancel(transaction):
            stored = await capture_ref.get(transaction=transaction)
            entitlement = await entitlement_ref.get(transaction=transaction)
            if not stored.exists:
                return
            key_hash = stored.get("idempotency_hash")
            transaction.delete(capture_ref)
            transaction.delete(
                self._collection(capture.account_id, "capture_idempotency").document(key_hash)
            )
            transaction.update(
                entitlement_ref,
                {
                    "accepted_image_count": max(0, entitlement.get("accepted_image_count") - 1),
                    "updated_at": utc_now(),
                },
            )

        await cancel(transaction)

    async def mark_processed(self, capture_id: str, account_id: str | None = None) -> None:
        if account_id is None:
            raise ValueError("Firestore capture updates require account scope")
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

    async def save_meal(self, meal: MealEntry) -> MealEntry:
        meal_ref = self._collection(meal.account_id, "meals").document(meal.id)
        capture_ref = self._collection(meal.account_id, "captures").document(meal.capture_id)
        revision = MealRevision(
            id=str(uuid4()),
            account_id=meal.account_id,
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
            if not capture.exists:
                raise CaptureNotFound
            existing_id = (capture.to_dict() or {}).get("meal_id")
            if existing_id:
                existing = (
                    await self._collection(meal.account_id, "meals")
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
        self, *, meal: MealEntry, prompt: str, reason: str
    ) -> ClarificationQuestion:
        reference = self._collection(meal.account_id, "questions").document(meal.id)
        existing = await reference.get()
        if existing.exists:
            return _model(existing, ClarificationQuestion)
        question = ClarificationQuestion(
            id=meal.id,
            account_id=meal.account_id,
            meal_id=meal.id,
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
        return account.model_copy(
            update={
                "trial_image_limit": entitlement.get("trial_image_limit"),
                "accepted_image_count": entitlement.get("accepted_image_count"),
            }
        )
