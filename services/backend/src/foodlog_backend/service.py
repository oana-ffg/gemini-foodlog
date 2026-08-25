from hashlib import sha256
from uuid import uuid4

from .errors import CrossAccountAccess
from .inference import InferenceEngine
from .models import (
    BrowserCamera,
    CaptureAccepted,
    CaptureEnvelopeV1,
    DeviceCamera,
    MealEntry,
)
from .repository import Repository
from .storage import ObjectStore


class CaptureService:
    def __init__(
        self,
        *,
        repository: Repository,
        object_store: ObjectStore,
        inference: InferenceEngine | None,
    ) -> None:
        self._repository = repository
        self._object_store = object_store
        self._inference = inference

    async def accept_browser_capture(
        self,
        *,
        owner_user_id: str,
        camera_id: str,
        idempotency_key: str,
        content_type: str,
        image: bytes,
        process_immediately: bool = True,
    ) -> CaptureAccepted:
        camera = await self._repository.camera_for_owner(owner_user_id, camera_id)
        return await self.accept_capture(
            owner_user_id=owner_user_id,
            camera=camera,
            idempotency_key=idempotency_key,
            content_type=content_type,
            image=image,
            metadata=None,
            process_immediately=process_immediately,
        )

    async def accept_capture(
        self,
        *,
        owner_user_id: str,
        camera: BrowserCamera | DeviceCamera,
        idempotency_key: str,
        content_type: str,
        image: bytes,
        metadata: CaptureEnvelopeV1 | None,
        process_immediately: bool = False,
    ) -> CaptureAccepted:
        account = await self._repository.account_for_owner(owner_user_id)
        if camera.account_id != account.id:
            raise CrossAccountAccess
        digest = sha256(image).hexdigest()
        extension = "jpg" if content_type == "image/jpeg" else "png"
        capture_id = str(uuid4())
        object_key = f"accounts/{account.id}/captures/{capture_id}.{extension}"
        capture, updated_account, created = await self._repository.reserve_capture(
            capture_id=capture_id,
            account=account,
            camera=camera,
            idempotency_key=idempotency_key,
            content_type=content_type,
            content_sha256=digest,
            object_key=object_key,
            metadata=metadata,
        )
        object_created = False
        try:
            object_created = await self._object_store.put(
                capture.object_key,
                image,
                content_type,
            )
            await self._repository.mark_stored(capture.id, capture.account_id)
            if created and process_immediately:
                await self._process_capture(
                    capture_id=capture.id,
                    account_id=account.id,
                    image=image,
                    content_type=content_type,
                )
        except Exception as error:
            cleanup_errors: list[Exception] = []
            if object_created:
                try:
                    await self._object_store.delete(capture.object_key)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if created:
                try:
                    await self._repository.cancel_capture(capture)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise ExceptionGroup(
                    "Capture acceptance and cleanup both failed",
                    [error, *cleanup_errors],
                ) from error
            raise
        return CaptureAccepted(
            capture_id=capture.id,
            accepted_image_count=updated_account.accepted_image_count,
            entitlement_mode=updated_account.entitlement_mode,
            trial_image_limit=updated_account.trial_image_limit,
            duplicate=not created,
        )

    async def _process_capture(
        self,
        *,
        capture_id: str,
        account_id: str,
        image: bytes,
        content_type: str,
    ) -> None:
        if self._inference is None:
            raise RuntimeError("No in-process inference engine is configured")
        inference = await self._inference.infer(image, content_type)
        meal = await self._repository.save_meal(
            MealEntry(
                **inference.model_dump(),
                id=str(uuid4()),
                account_id=account_id,
                capture_id=capture_id,
            )
        )
        if inference.clarification_question and inference.clarification_reason:
            await self._repository.open_question(
                meal=meal,
                prompt=inference.clarification_question,
                reason=inference.clarification_reason,
            )
        await self._repository.mark_processed(capture_id, account_id)
