import asyncio
from datetime import timedelta
from hashlib import sha256

import pytest

from foodlog_backend.errors import (
    AccountAlreadyProvisioned,
    AccountCapacityReached,
    AccountCapacityStateConflict,
    AccountNotProvisioned,
    CameraNotFound,
    InvalidDeviceCredential,
)
from foodlog_backend.firestore_repository import (
    public_capacity_state_problems,
    public_capacity_values,
)
from foodlog_backend.models import (
    AccountCapacityAction,
    AccountCapacityReason,
    AccountExportStatus,
    AuditAction,
    Confidence,
    DurableJob,
    EntitlementMode,
    JobKind,
    JobStatus,
    MealEntry,
    utc_now,
)
from foodlog_backend.repository import InMemoryRepository

RECLAIM_OPERATION_ID = "11111111-1111-4111-8111-111111111111"
RESTORE_OPERATION_ID = "22222222-2222-4222-8222-222222222222"


class CapacitySnapshot:
    exists = True

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def get(self, field: str):
        return self._data[field]

    def to_dict(self) -> dict[str, object]:
        return dict(self._data)


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (
            {"active_account_count": 10, "account_limit": 30, "waitlist_open": False},
            "limit_mismatch",
        ),
        (
            {"active_account_count": 26, "account_limit": 25, "waitlist_open": True},
            "count_exceeds_limit",
        ),
        (
            {"active_account_count": 25, "account_limit": 25, "waitlist_open": False},
            "waitlist_open_mismatch",
        ),
        (
            {"active_account_count": True, "account_limit": 25, "waitlist_open": True},
            "count_invalid",
        ),
    ],
)
def test_malformed_public_capacity_state_fails_closed(
    data: dict[str, object],
    problem: str,
) -> None:
    snapshot = CapacitySnapshot(data)

    assert problem in public_capacity_state_problems(  # type: ignore[arg-type]
        snapshot,
        configured_limit=25,
    )
    with pytest.raises(ValueError, match="Public account capacity state is invalid"):
        public_capacity_values(snapshot, configured_limit=25)  # type: ignore[arg-type]


def test_reclaim_is_audited_idempotent_and_fences_account_work() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=2, trial_image_limit=200)
        account = await repository.provision_account(
            "capacity-owner-a",
            verified_email_normalized="capacity-owner-a@example.test",
        )
        await repository.provision_account(
            "capacity-owner-b",
            verified_email_normalized="capacity-owner-b@example.test",
        )
        job = DurableJob(
            id="capacity-job-a",
            account_id=account.id,
            kind=JobKind.CAPTURE_GROUPING,
            subject_id="capture-a",
            subject_revision=1,
        )
        await repository.enqueue_job(job)

        reclaimed = await repository.change_public_account_capacity(
            account_id=account.id,
            action=AccountCapacityAction.RECLAIM,
            reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
            operation_id=RECLAIM_OPERATION_ID,
        )
        assert reclaimed.previous_status == "active"
        assert reclaimed.resulting_status == "capacity_reclaimed"
        assert reclaimed.active_public_account_count == 1
        assert (
            await repository.change_public_account_capacity(
                account_id=account.id,
                action=AccountCapacityAction.RECLAIM,
                reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
                operation_id=RECLAIM_OPERATION_ID,
            )
            == reclaimed
        )
        with pytest.raises(AccountCapacityStateConflict):
            await repository.change_public_account_capacity(
                account_id=account.id,
                action=AccountCapacityAction.RECLAIM,
                reason=AccountCapacityReason.MISSING_FIREBASE_IDENTITY,
                operation_id=RECLAIM_OPERATION_ID,
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.account_for_owner(account.owner_user_id)
        assert (
            await repository.claim_job(
                account_id=account.id,
                job_id=job.id,
                expected_subject_revision=1,
                lease_id="capacity-lease-a",
                lease_owner="capacity-test-worker",
                lease_expires_at=utc_now() + timedelta(minutes=1),
            )
            is None
        )

    asyncio.run(scenario())


def test_reclaimed_slot_fulfils_waitlist_and_restore_is_capacity_safe() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=2, trial_image_limit=200)
        account_a = await repository.provision_account("capacity-owner-a")
        account_b = await repository.provision_account("capacity-owner-b")
        await repository.join_waitlist(
            firebase_uid="capacity-owner-c",
            email_normalized="capacity-owner-c@example.test",
            policy_version="capacity-waitlist-v1",
        )
        await repository.change_public_account_capacity(
            account_id=account_a.id,
            action=AccountCapacityAction.RECLAIM,
            reason=AccountCapacityReason.MISSING_FIREBASE_IDENTITY,
            operation_id=RECLAIM_OPERATION_ID,
        )
        await repository.provision_account(
            "capacity-owner-c",
            verified_email_normalized="capacity-owner-c@example.test",
        )
        preferences = await repository.consent_preferences(firebase_uid="capacity-owner-c")
        assert preferences.waitlist_status == "fulfilled"
        with pytest.raises(AccountAlreadyProvisioned):
            await repository.withdraw_waitlist(firebase_uid="capacity-owner-c")
        with pytest.raises(AccountAlreadyProvisioned):
            await repository.join_waitlist(
                firebase_uid="capacity-owner-c",
                email_normalized="changed-capacity-owner-c@example.test",
                policy_version="capacity-waitlist-v1",
            )
        with pytest.raises(AccountCapacityStateConflict):
            await repository.change_public_account_capacity(
                account_id=account_a.id,
                action=AccountCapacityAction.RESTORE,
                reason=AccountCapacityReason.OPERATOR_REVERSAL,
                operation_id=RESTORE_OPERATION_ID,
            )
        await repository.change_public_account_capacity(
            account_id=account_b.id,
            action=AccountCapacityAction.RECLAIM,
            reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
            operation_id="33333333-3333-4333-8333-333333333333",
        )
        restored = await repository.change_public_account_capacity(
            account_id=account_a.id,
            action=AccountCapacityAction.RESTORE,
            reason=AccountCapacityReason.OPERATOR_REVERSAL,
            operation_id=RESTORE_OPERATION_ID,
        )
        assert restored.resulting_status == "active"
        assert await repository.account_for_owner(account_a.owner_user_id) == account_a
        audit = await repository.list_audit_events_for_owner(account_a.owner_user_id)
        assert {event.action for event in audit} >= {
            AuditAction.ACCOUNT_CAPACITY_RECLAIMED,
            AuditAction.ACCOUNT_CAPACITY_RESTORED,
        }

    asyncio.run(scenario())


def test_internal_accounts_cannot_consume_or_release_public_capacity() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(
            public_account_limit=1,
            trial_image_limit=200,
            unlimited_owner_user_ids={"internal-owner"},
        )
        account = await repository.provision_account("internal-owner")
        assert account.entitlement_mode == EntitlementMode.UNLIMITED
        with pytest.raises(AccountCapacityStateConflict):
            await repository.change_public_account_capacity(
                account_id=account.id,
                action=AccountCapacityAction.RECLAIM,
                reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
                operation_id=RECLAIM_OPERATION_ID,
            )

    asyncio.run(scenario())


def test_reclaim_racing_with_replacement_admission_keeps_exact_capacity() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=1, trial_image_limit=200)
        original = await repository.provision_account("capacity-race-original")

        reclaim, admission = await asyncio.gather(
            repository.change_public_account_capacity(
                account_id=original.id,
                action=AccountCapacityAction.RECLAIM,
                reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
                operation_id=RECLAIM_OPERATION_ID,
            ),
            repository.provision_account(
                "capacity-race-replacement",
                verified_email_normalized="capacity-race-replacement@example.test",
            ),
            return_exceptions=True,
        )

        assert not isinstance(reclaim, Exception)
        if isinstance(admission, Exception):
            assert isinstance(admission, AccountCapacityReached)
            admission = await repository.provision_account(
                "capacity-race-replacement",
                verified_email_normalized="capacity-race-replacement@example.test",
            )
        assert admission.owner_user_id == "capacity-race-replacement"
        active_public_count = sum(
            account.entitlement_mode == EntitlementMode.TRIAL
            and repository._account_status_by_id[account.id] == "active"
            for account in repository._accounts.values()
        )
        assert active_public_count == 1

    asyncio.run(scenario())


def test_reserved_capture_cannot_be_cancelled_after_reclaim() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=1, trial_image_limit=200)
        account = await repository.provision_account("capacity-capture-owner")
        camera = await repository.create_browser_camera(
            account.owner_user_id,
            "Capacity camera",
            "capacity-browser-instance-0001",
        )
        capture, _, created = await repository.reserve_capture(
            capture_id="capacity-pending-capture",
            account=account,
            camera=camera,
            idempotency_key="capacity-pending-idempotency",
            content_type="image/jpeg",
            content_sha256=sha256(b"pending capture").hexdigest(),
            object_key=f"accounts/{account.id}/captures/capacity-pending-capture.jpg",
        )
        assert created is True
        await repository.change_public_account_capacity(
            account_id=account.id,
            action=AccountCapacityAction.RECLAIM,
            reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
            operation_id=RECLAIM_OPERATION_ID,
        )

        with pytest.raises(AccountNotProvisioned):
            await repository.cancel_capture(account_id=account.id, capture=capture)
        assert repository._captures[capture.id] == capture
        assert repository._accounts[account.id].accepted_image_count == 1

    asyncio.run(scenario())


def test_export_error_paths_cannot_mutate_after_reclaim() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=1, trial_image_limit=200)
        account = await repository.provision_account("capacity-export-owner")
        requested_at = utc_now()
        account_export, created = await repository.create_account_export(
            owner_user_id=account.owner_user_id,
            idempotency_key="capacity-export-request",
            requested_at=requested_at,
            cooldown=timedelta(hours=1),
        )
        assert created is True
        lease_id = "capacity-export-lease"
        claimed = await repository.claim_account_export(
            account_id=account.id,
            export_id=account_export.id,
            lease_id=lease_id,
            lease_owner="capacity-export-worker",
            lease_expires_at=requested_at + timedelta(hours=1),
        )
        assert claimed is not None
        await repository.change_public_account_capacity(
            account_id=account.id,
            action=AccountCapacityAction.RECLAIM,
            reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
            operation_id=RECLAIM_OPERATION_ID,
        )

        assert not await repository.release_account_export(
            account_id=account.id,
            export_id=account_export.id,
            lease_id=lease_id,
            available_at=requested_at + timedelta(minutes=1),
            error_code="SyntheticRetry",
        )
        assert not await repository.fail_account_export(
            account_id=account.id,
            export_id=account_export.id,
            lease_id=lease_id,
            error_code="SyntheticFailure",
            failed_at=requested_at + timedelta(minutes=1),
        )
        frozen_export = repository._account_exports[(account.id, account_export.id)]
        frozen_job = repository._jobs[(account.id, account_export.job_id)]
        assert frozen_export.status == AccountExportStatus.BUILDING
        assert frozen_job.status == JobStatus.LEASED
        assert frozen_job.lease_id == lease_id

    asyncio.run(scenario())


def test_reclaimed_inmemory_account_blocks_device_mail_export_and_leased_jobs() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=1, trial_image_limit=200)
        account = await repository.provision_account("capacity-parity-owner")
        credential_hash = sha256(b"capacity-device-credential").hexdigest()
        device_camera = await repository.issue_device_camera(
            owner_user_id=account.owner_user_id,
            name="Capacity device",
            credential_hash=credential_hash,
            token_version=1,
        )
        browser_camera = await repository.create_browser_camera(
            account.owner_user_id,
            "Capacity browser camera",
            "capacity-browser-instance",
        )
        capture_id = "capacity-reclaim-publication-capture"
        capture, _, _ = await repository.reserve_capture(
            capture_id=capture_id,
            account=account,
            camera=browser_camera,
            idempotency_key="capacity-reclaim-publication-capture-key",
            content_type="image/jpeg",
            content_sha256="a" * 64,
            object_key=f"accounts/{account.id}/captures/{capture_id}.jpg",
        )
        await repository.mark_stored(account_id=account.id, capture_id=capture.id)
        domain = "gemini-foodlog-2026.appspotmail.com"
        address = f"f-{'a' * 48}@{domain}"
        address_hash = sha256(address.encode()).hexdigest()
        inbound = await repository.create_inbound_mail_address(
            owner_user_id=account.owner_user_id,
            address=address,
            address_hash=address_hash,
        )
        job = DurableJob(
            id="capacity-already-leased-job",
            account_id=account.id,
            kind=JobKind.CAPTURE_GROUPING,
            subject_id="capacity-leased-capture",
            subject_revision=1,
        )
        await repository.enqueue_job(job)
        lease_id = "capacity-already-active-lease"
        lease_owner = "capacity-test-worker"
        claimed = await repository.claim_job(
            account_id=account.id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id=lease_id,
            lease_owner=lease_owner,
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
        assert claimed is not None
        await repository.change_public_account_capacity(
            account_id=account.id,
            action=AccountCapacityAction.RECLAIM,
            reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
            operation_id=RECLAIM_OPERATION_ID,
        )

        with pytest.raises(InvalidDeviceCredential):
            await repository.authenticate_device(credential_hash)
        with pytest.raises(CameraNotFound):
            await repository.device_camera_for_identity(
                account_id=account.id,
                camera_id=device_camera.id,
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.create_account_export(
                owner_user_id=account.owner_user_id,
                idempotency_key="blocked-export-after-reclaim",
                requested_at=utc_now(),
                cooldown=timedelta(hours=1),
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.record_launch_mail_consent(
                owner_user_id=account.owner_user_id,
                email_normalized="capacity-parity-owner@example.test",
                granted=True,
                policy_version="launch-mail-v1",
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.consent_preferences(firebase_uid=account.owner_user_id)
        with pytest.raises(AccountNotProvisioned):
            await repository.create_inbound_mail_address(
                owner_user_id=account.owner_user_id,
                address=address,
                address_hash=address_hash,
            )
        replacement = f"f-{'b' * 48}@{domain}"
        with pytest.raises(AccountNotProvisioned):
            await repository.rotate_inbound_mail_address(
                owner_user_id=account.owner_user_id,
                expected_generation=inbound.generation,
                address=replacement,
                address_hash=sha256(replacement.encode()).hexdigest(),
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.revoke_inbound_mail_address(
                owner_user_id=account.owner_user_id,
                expected_generation=inbound.generation,
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.enqueue_job(
                DurableJob(
                    id="capacity-new-job-after-reclaim",
                    account_id=account.id,
                    kind=JobKind.CAPTURE_GROUPING,
                    subject_id="capacity-new-capture-after-reclaim",
                    subject_revision=1,
                )
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.mark_processed(account_id=account.id, capture_id=capture.id)
        with pytest.raises(AccountNotProvisioned):
            await repository.save_meal(
                account_id=account.id,
                meal=MealEntry(
                    id="capacity-meal-after-reclaim",
                    account_id=account.id,
                    capture_id=capture.id,
                    title="Steak",
                    confidence=Confidence.LIKELY,
                    components=[],
                    observations=["Red meat is visible."],
                    alternatives=["Chicken"],
                    rationale="The meat appears red.",
                ),
            )
        assert not await repository.complete_job(
            account_id=account.id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id=lease_id,
            lease_owner=lease_owner,
        )
        assert not await repository.release_job(
            account_id=account.id,
            job_id=job.id,
            expected_subject_revision=1,
            lease_id=lease_id,
            lease_owner=lease_owner,
            available_at=utc_now(),
            error_code="SyntheticFailure",
            error_message="must remain frozen",
        )
        frozen = repository._jobs[(account.id, job.id)]
        assert frozen.status == JobStatus.LEASED
        assert frozen.lease_id == lease_id

    asyncio.run(scenario())
