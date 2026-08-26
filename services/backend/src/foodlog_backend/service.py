from hashlib import sha256
from uuid import uuid4

from .errors import CrossAccountAccess
from .image_events import CaptureEventPublisher, CaptureStoredEventV1
from .models import (
    BrowserCamera,
    CaptureAccepted,
    CaptureEnvelopeV1,
    DeviceCamera,
)
from .repository import Repository
from .storage import ObjectStore


class CaptureService:
    def __init__(
        self,
        *,
        repository: Repository,
        object_store: ObjectStore,
        event_publisher: CaptureEventPublisher,
    ) -> None:
        self._repository = repository
        self._object_store = object_store
        self._event_publisher = event_publisher

    async def accept_capture(
        self,
        *,
        owner_user_id: str,
        camera: BrowserCamera | DeviceCamera,
        idempotency_key: str,
        content_type: str,
        image: bytes,
        metadata: CaptureEnvelopeV1 | None,
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
                capture.account_id,
                capture.object_key,
                image,
                content_type,
            )
            await self._repository.mark_stored(
                account_id=capture.account_id,
                capture_id=capture.id,
            )
            await self._event_publisher.publish(
                CaptureStoredEventV1(
                    account_id=capture.account_id,
                    capture_id=capture.id,
                )
            )
        except Exception as error:
            cleanup_errors: list[Exception] = []
            # A successfully created object is immutable evidence. Keep its
            # reservation so the same idempotent retry can finish Firestore
            # finalization without requiring broad object-delete permission.
            if created and not object_created:
                try:
                    await self._repository.cancel_capture(
                        account_id=capture.account_id,
                        capture=capture,
                    )
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
