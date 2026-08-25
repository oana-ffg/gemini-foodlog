from hashlib import sha256
from uuid import uuid4

from .inference import InferenceEngine
from .models import CaptureAccepted, MealEntry
from .repository import Repository
from .storage import ObjectStore


class CaptureService:
    def __init__(
        self,
        *,
        repository: Repository,
        object_store: ObjectStore,
        inference: InferenceEngine,
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
    ) -> CaptureAccepted:
        account = await self._repository.account_for_owner(owner_user_id)
        camera = await self._repository.camera_for_owner(owner_user_id, camera_id)
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
        )
        if created:
            try:
                await self._object_store.put(capture.object_key, image, content_type)
                inference = await self._inference.infer(image, content_type)
                meal = await self._repository.save_meal(
                    MealEntry(
                        **inference.model_dump(),
                        id=str(uuid4()),
                        account_id=account.id,
                        capture_id=capture.id,
                    )
                )
                if inference.clarification_question and inference.clarification_reason:
                    await self._repository.open_question(
                        meal=meal,
                        prompt=inference.clarification_question,
                        reason=inference.clarification_reason,
                    )
                await self._repository.mark_processed(capture.id, capture.account_id)
            except Exception:
                try:
                    await self._object_store.delete(capture.object_key)
                finally:
                    await self._repository.cancel_capture(capture)
                raise
        return CaptureAccepted(
            capture_id=capture.id,
            accepted_image_count=updated_account.accepted_image_count,
            trial_image_limit=updated_account.trial_image_limit,
            duplicate=not created,
        )
