import argparse
import asyncio
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from foodlog_backend.errors import CaptureNotFound, CrossAccountAccess
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.storage import GCSObjectStore


async def smoke(args: argparse.Namespace) -> None:
    image = args.fixture.read_bytes()
    content_sha256 = sha256(image).hexdigest()
    repository = FirestoreRepository(
        project_id=args.project,
        public_account_limit=25,
        trial_image_limit=200,
    )
    store = GCSObjectStore(project_id=args.project, bucket_name=args.bucket)
    provisioned_accounts = await asyncio.gather(
        *(repository.provision_account(args.owner_id) for _ in range(10))
    )
    assert len({account.id for account in provisioned_accounts}) == 1
    account = provisioned_accounts[0]
    camera = await repository.create_browser_camera(
        args.owner_id,
        "Durable smoke camera",
        "durable-smoke-browser-instance-0001",
    )
    idempotency_key = f"durable-smoke-{content_sha256[:24]}"

    async def reserve() -> tuple:
        candidate_id = str(uuid4())
        return await repository.reserve_capture(
            capture_id=candidate_id,
            account=account,
            camera=camera,
            idempotency_key=idempotency_key,
            content_type="image/png",
            content_sha256=content_sha256,
            object_key=f"accounts/{account.id}/captures/{candidate_id}.png",
        )

    first, second = await asyncio.gather(reserve(), reserve())
    created_results = [result for result in (first, second) if result[2]]
    assert len(created_results) in {0, 1}
    assert first[0].id == second[0].id
    assert first[1].accepted_image_count == second[1].accepted_image_count == 1

    capture = created_results[0][0] if created_results else first[0]
    if created_results:
        await store.put(account.id, capture.object_key, image, capture.content_type)
    assert sha256(await store.get(account.id, capture.object_key)).hexdigest() == content_sha256
    try:
        await store.get("foreign-account", capture.object_key)
    except CrossAccountAccess:
        pass
    else:
        raise AssertionError("foreign account object read reached storage")
    try:
        await store.put(
            account.id,
            f"accounts/foreign-account/captures/{capture.id}.png",
            image,
            capture.content_type,
        )
    except CrossAccountAccess:
        pass
    else:
        raise AssertionError("foreign account object write reached storage")
    await repository.mark_processed(account_id=account.id, capture_id=capture.id)

    stored = await repository.capture_for_owner(args.owner_id, capture.id)
    assert stored.status.value == "processed"

    rollback_id = str(uuid4())
    rollback, reserved_account, created = await repository.reserve_capture(
        capture_id=rollback_id,
        account=account,
        camera=camera,
        idempotency_key=f"rollback-smoke-{rollback_id}",
        content_type="image/png",
        content_sha256=content_sha256,
        object_key=f"accounts/{account.id}/captures/{rollback_id}.png",
    )
    assert created and reserved_account.accepted_image_count == 2
    await repository.cancel_capture(account_id=account.id, capture=rollback)
    assert (await repository.account_for_owner(args.owner_id)).accepted_image_count == 1
    try:
        await repository.capture_for_owner(args.owner_id, rollback.id)
    except CaptureNotFound:
        pass
    else:
        raise AssertionError("rolled-back capture still exists")

    print(f"account_id={account.id}")
    print(f"camera_id={camera.id}")
    print(f"capture_id={capture.id}")
    print(f"object_key={capture.object_key}")
    print(f"sha256={content_sha256}")
    print("accepted_image_count=1")
    print("account_provisioning_idempotent=true")
    print("rollback_verified=true")
    print("cross_account_storage_denied=true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--fixture", required=True, type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(smoke(parse_args()))
