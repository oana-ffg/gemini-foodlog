from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.errors import (
    AccountAlreadyProvisioned,
    AccountCapacityReached,
    AccountCapacityStateConflict,
    AccountExportAlreadyActive,
    AccountExportNotFound,
    AccountNotProvisioned,
    AiTraceConflict,
    AiTraceNotFound,
    CameraNotFound,
    DeviceSnapshotNotFound,
    IdempotencyConflict,
    InboundAddressStateConflict,
    InvalidMealFeedbackTransition,
    KnowledgePageNotFound,
    KnowledgeRevisionConflict,
    MealNotFound,
    MealRevisionConflict,
    QuestionAlreadyAnswered,
    QuestionNotFound,
    QuestionSuperseded,
    UserContextNoteNotFound,
)
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.firestore_repository_smoke import (
    SMOKE_ACCOUNT_ID,
    ensure_smoke_fixture,
    run_smoke,
)
from foodlog_backend.inbound_mail import InboundMailAddressService, inbound_recipient_hash
from foodlog_backend.inference_schema import ActivityMealInferenceV1
from foodlog_backend.models import (
    AccountCapacityAction,
    AccountCapacityReason,
    ActivityEvent,
    ActivityEventStatus,
    AiTraceRecord,
    CaptureRecord,
    CaptureStatus,
    Confidence,
    DeviceSnapshotStatus,
    DurableJob,
    JobKind,
    JobStatus,
    KnowledgeBeliefStrength,
    KnowledgeClaim,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeLifecycle,
    KnowledgeRevisionDraft,
    KnowledgeRevisionResult,
    KnowledgeRevisionSource,
    MealComponent,
    MealEntry,
    MealFeedbackKind,
    MealFeedbackRequest,
    MealStatus,
    QuestionAnswerRequest,
    QuestionAnswerResult,
    QuestionEvidenceKind,
    QuestionEvidenceReference,
    QuestionResponseKind,
    QuestionResponseRequest,
    QuestionResponseResult,
    QuestionStatus,
    UserContextNoteCreate,
    UserContextNoteStatus,
    account_export_id,
    event_inference_job_id,
    utc_now,
)
from tests.inference_fixtures import base_payload


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_manual_snapshot_commands_are_atomic_idempotent_and_scoped() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-snapshot-contract-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        account = await repository.provision_account("snapshot-contract-owner")
        await repository.provision_account("snapshot-contract-other")
        camera = await repository.issue_device_camera(
            owner_user_id=account.owner_user_id,
            name="Counter camera",
            credential_hash=sha256(b"snapshot-contract-credential").hexdigest(),
            token_version=1,
        )

        requested = await repository.request_device_snapshot(
            owner_user_id=account.owner_user_id,
            camera_id=camera.id,
        )
        retry = await repository.request_device_snapshot(
            owner_user_id=account.owner_user_id,
            camera_id=camera.id,
        )
        assert retry == requested
        assert requested.status == DeviceSnapshotStatus.PENDING
        assert await repository.pending_device_snapshot(
            account_id=account.id,
            camera_id=camera.id,
        ) == requested

        with pytest.raises(CameraNotFound):
            await repository.request_device_snapshot(
                owner_user_id="snapshot-contract-other",
                camera_id=camera.id,
            )
        with pytest.raises(DeviceSnapshotNotFound):
            await repository.device_snapshot_for_owner(
                owner_user_id="snapshot-contract-other",
                camera_id=camera.id,
                request_id=requested.id,
            )

        completed = await repository.complete_device_snapshot(
            account_id=account.id,
            camera_id=camera.id,
            request_id=requested.id,
            capture_id="snapshot-contract-capture",
        )
        assert completed.status == DeviceSnapshotStatus.COMPLETED
        assert completed.capture_id == "snapshot-contract-capture"
        assert await repository.complete_device_snapshot(
            account_id=account.id,
            camera_id=camera.id,
            request_id=requested.id,
            capture_id="snapshot-contract-capture",
        ) == completed
        assert await repository.pending_device_snapshot(
            account_id=account.id,
            camera_id=camera.id,
        ) is None
        await client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_inbound_address_rotation_is_atomic_under_competing_requests() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-mail-address-race-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        account = await repository.provision_account("mail-address-race-owner")
        tokens = iter(("a" * 48, "b" * 48, "c" * 48))
        domain = "gemini-foodlog-2026.appspotmail.com"
        service = InboundMailAddressService(
            repository=repository,
            domain=domain,
            token_factory=lambda: next(tokens),
        )
        initial = await service.get_or_create(account.owner_user_id)

        outcomes = await asyncio.gather(
            service.rotate(account.owner_user_id, expected_generation=initial.generation),
            service.rotate(account.owner_user_id, expected_generation=initial.generation),
            return_exceptions=True,
        )
        accepted = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
        rejected = [outcome for outcome in outcomes if isinstance(outcome, Exception)]

        assert len(accepted) == 1
        assert len(rejected) == 1
        assert isinstance(rejected[0], InboundAddressStateConflict)
        replacement = accepted[0]
        assert replacement.generation == 2
        assert await service.get_or_create(account.owner_user_id) == replacement

        old_route = (
            await client.collection("inbound_mail_routes")
            .document(inbound_recipient_hash(initial.address, expected_domain=domain))
            .get()
        )
        replacement_route = (
            await client.collection("inbound_mail_routes")
            .document(inbound_recipient_hash(replacement.address, expected_domain=domain))
            .get()
        )
        assert old_route.get("status") == "revoked"
        assert old_route.get("generation") == 1
        assert replacement_route.get("status") == "active"
        assert replacement_route.get("generation") == 2
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_account_export_request_is_atomic_idempotent_and_scoped() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-export-contract-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        account = await repository.provision_account("firestore-export-owner")
        await repository.provision_account("firestore-export-other")
        requested_at = utc_now()
        request_keys = [f"firestore-export-{index:04d}" for index in range(10)]
        outcomes = await asyncio.gather(
            *(
                repository.create_account_export(
                    owner_user_id=account.owner_user_id,
                    idempotency_key=request_key,
                    requested_at=requested_at,
                    cooldown=timedelta(hours=1),
                )
                for request_key in request_keys
            ),
            return_exceptions=True,
        )
        accepted = [result for result in outcomes if not isinstance(result, Exception)]
        rejected = [result for result in outcomes if isinstance(result, Exception)]
        assert len(accepted) == 1
        assert len(rejected) == 9
        assert all(isinstance(error, AccountExportAlreadyActive) for error in rejected)
        account_export, created = accepted[0]
        assert created is True

        retry, retry_created = await repository.create_account_export(
            owner_user_id=account.owner_user_id,
            idempotency_key=next(
                request_key
                for request_key in request_keys
                if account_export.id == account_export_id(account.id, request_key)
            ),
            requested_at=requested_at + timedelta(minutes=1),
            cooldown=timedelta(hours=1),
        )
        assert retry_created is False
        assert retry == account_export
        assert (
            await repository.account_export_for_owner(
                owner_user_id=account.owner_user_id,
                export_id=account_export.id,
            )
            == account_export
        )
        with pytest.raises(AccountExportNotFound):
            await repository.account_export_for_owner(
                owner_user_id="firestore-export-other",
                export_id=account_export.id,
            )

        account_ref = client.collection("accounts").document(account.id)
        export_snapshot = await account_ref.collection("exports").document(account_export.id).get()
        job_snapshot = await account_ref.collection("jobs").document(account_export.job_id).get()
        audit_snapshots = [
            snapshot async for snapshot in account_ref.collection("audit_events").stream()
        ]
        control_snapshot = await account_ref.collection("export_control").document("current").get()
        assert export_snapshot.exists
        assert job_snapshot.get("kind") == JobKind.ACCOUNT_EXPORT.value
        assert job_snapshot.get("subject_id") == account_export.id
        assert len(audit_snapshots) == 1
        assert audit_snapshots[0].get("action") == "account_export.requested"
        assert control_snapshot.get("active_export_id") == account_export.id
        assert "firestore-export" not in repr(export_snapshot.to_dict())

        lease_id = "firestore-export-completion-lease"
        claimed = await repository.claim_account_export(
            account_id=account.id,
            export_id=account_export.id,
            lease_id=lease_id,
            lease_owner="contract-worker",
            lease_expires_at=utc_now() + timedelta(minutes=5),
        )
        assert claimed is not None
        completed_at = utc_now()
        completed = await repository.complete_account_export(
            account_id=account.id,
            export_id=account_export.id,
            lease_id=lease_id,
            archive_object_key=f"accounts/{account.id}/exports/{account_export.id}.zip",
            archive_size=123,
            archive_sha256="a" * 64,
            manifest_sha256="b" * 64,
            completed_at=completed_at,
            expires_at=completed_at + timedelta(hours=24),
        )
        assert completed is not None
        completed_control = await (
            account_ref.collection("export_control").document("current").get()
        )
        assert completed_control.exists
        assert "active_export_id" not in (completed_control.to_dict() or {})

        next_export, next_created = await repository.create_account_export(
            owner_user_id=account.owner_user_id,
            idempotency_key="firestore-export-after-completion",
            requested_at=requested_at + timedelta(hours=1, seconds=1),
            cooldown=timedelta(hours=1),
        )
        assert next_created is True
        assert next_export.id != account_export.id

        await client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_public_capacity_is_atomic_and_waitlist_is_stable() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-capacity-contract-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=4,
            trial_image_limit=200,
            client=client,
        )

        owner_ids = [f"capacity-contract-owner-{index}" for index in range(8)]
        outcomes = await asyncio.gather(
            *(
                repository.provision_account(
                    owner_id,
                    verified_email_normalized=f"{owner_id}@example.test",
                )
                for owner_id in owner_ids
            ),
            return_exceptions=True,
        )
        admitted = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
        rejected = [outcome for outcome in outcomes if isinstance(outcome, Exception)]

        assert len(admitted) == 4, repr(rejected)
        assert len({account.id for account in admitted}) == 4
        assert len(rejected) == 4
        assert all(isinstance(error, AccountCapacityReached) for error in rejected), repr(rejected)

        admitted_owner_id = admitted[0].owner_user_id
        assert await repository.provision_account(admitted_owner_id) == admitted[0]
        assert (
            await repository.provision_account(
                admitted_owner_id,
                verified_email_normalized="changed-email@example.test",
            )
            == admitted[0]
        )
        initial_preferences = await repository.consent_preferences(
            firebase_uid=admitted_owner_id,
        )
        assert initial_preferences.launch_mail_opt_in is None
        launch_grant = await repository.record_launch_mail_consent(
            owner_user_id=admitted_owner_id,
            email_normalized=f"{admitted_owner_id}@example.test",
            granted=True,
            policy_version="launch-interest-v1",
        )
        launch_withdrawal = await repository.record_launch_mail_consent(
            owner_user_id=admitted_owner_id,
            email_normalized=f"{admitted_owner_id}@example.test",
            granted=False,
            policy_version="launch-interest-v1",
        )
        assert launch_grant.id != launch_withdrawal.id
        assert (
            await repository.consent_preferences(firebase_uid=admitted_owner_id)
        ).launch_mail_opt_in is False
        assert (
            await repository.record_launch_mail_consent(
                owner_user_id=admitted_owner_id,
                email_normalized=f"{admitted_owner_id}@example.test",
                granted=True,
                policy_version="launch-interest-v1",
            )
            == launch_grant
        )
        assert (
            await repository.consent_preferences(firebase_uid=admitted_owner_id)
        ).launch_mail_opt_in is True

        rejected_owner_id = next(
            owner_id
            for owner_id, outcome in zip(owner_ids, outcomes, strict=True)
            if isinstance(outcome, AccountCapacityReached)
        )
        waitlist = await repository.join_waitlist(
            firebase_uid=rejected_owner_id,
            email_normalized=f"{rejected_owner_id}@example.test",
            policy_version="capacity-waitlist-v1",
        )
        assert (
            await repository.join_waitlist(
                firebase_uid=rejected_owner_id,
                email_normalized=f"{rejected_owner_id}@example.test",
                policy_version="capacity-waitlist-v1",
            )
            == waitlist
        )
        assert (
            await repository.consent_preferences(firebase_uid=rejected_owner_id)
        ).waitlist_status == "active"
        withdrawn = await repository.withdraw_waitlist(firebase_uid=rejected_owner_id)
        assert withdrawn.status == "withdrawn"
        assert withdrawn.email_normalized is None
        assert withdrawn.mailing_list_opt_in is False
        assert withdrawn.withdrawal_count == 1
        assert await repository.withdraw_waitlist(firebase_uid=rejected_owner_id) == withdrawn
        assert (
            await repository.consent_preferences(firebase_uid=rejected_owner_id)
        ).waitlist_status == "withdrawn"
        waitlist_snapshot = (
            await client.collection("waitlist")
            .document(sha256(rejected_owner_id.encode()).hexdigest())
            .get()
        )
        assert waitlist_snapshot.exists
        assert "firebase_uid" not in waitlist_snapshot.to_dict()
        assert waitlist_snapshot.get("email_normalized") is None
        rejoined = await repository.join_waitlist(
            firebase_uid=rejected_owner_id,
            email_normalized=f"{rejected_owner_id}@example.test",
            policy_version="capacity-waitlist-v1",
        )
        assert rejoined.id == waitlist.id
        assert rejoined.status == "active"
        assert rejoined.withdrawal_count == 1
        assert rejoined.last_withdrawn_at == withdrawn.last_withdrawn_at
        with pytest.raises(AccountAlreadyProvisioned):
            await repository.join_waitlist(
                firebase_uid=admitted_owner_id,
                email_normalized=f"{admitted_owner_id}@example.test",
                policy_version="capacity-waitlist-v1",
            )

        reclaimed_account = admitted[0]
        reclaimed_device = await repository.issue_device_camera(
            owner_user_id=reclaimed_account.owner_user_id,
            name="Reclaimed device",
            credential_hash=sha256(b"reclaimed-device-credential").hexdigest(),
            token_version=1,
        )
        reclaimed_browser = await repository.create_browser_camera(
            reclaimed_account.owner_user_id,
            "Reclaimed browser",
            "reclaimed-browser-instance",
        )
        reclaimed_capture_id = "firestore-reclaimed-capture"
        reclaimed_capture, _, _ = await repository.reserve_capture(
            capture_id=reclaimed_capture_id,
            account=reclaimed_account,
            camera=reclaimed_browser,
            idempotency_key="firestore-reclaimed-capture-key",
            content_type="image/jpeg",
            content_sha256="a" * 64,
            object_key=(
                f"accounts/{reclaimed_account.id}/captures/{reclaimed_capture_id}.jpg"
            ),
        )
        await repository.mark_stored(
            account_id=reclaimed_account.id,
            capture_id=reclaimed_capture.id,
        )

        reclaimed = await repository.change_public_account_capacity(
            account_id=admitted[0].id,
            action=AccountCapacityAction.RECLAIM,
            reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
            operation_id="11111111-1111-4111-8111-111111111111",
        )
        assert reclaimed.active_public_account_count == 3
        with pytest.raises(AccountNotProvisioned):
            await repository.account_for_owner(reclaimed_account.owner_user_id)
        with pytest.raises(AccountNotProvisioned):
            await repository.consent_preferences(
                firebase_uid=reclaimed_account.owner_user_id,
            )
        with pytest.raises(CameraNotFound):
            await repository.device_camera_for_identity(
                account_id=reclaimed_account.id,
                camera_id=reclaimed_device.id,
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.enqueue_job(
                DurableJob(
                    id="firestore-job-after-reclaim",
                    account_id=reclaimed_account.id,
                    kind=JobKind.CAPTURE_GROUPING,
                    subject_id="firestore-capture-after-reclaim",
                    subject_revision=1,
                )
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.mark_processed(
                account_id=reclaimed_account.id,
                capture_id=reclaimed_capture.id,
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.save_meal(
                account_id=reclaimed_account.id,
                meal=MealEntry(
                    id="firestore-meal-after-reclaim",
                    account_id=reclaimed_account.id,
                    capture_id=reclaimed_capture.id,
                    title="Steak",
                    confidence=Confidence.LIKELY,
                    components=[],
                    observations=["Red meat is visible."],
                    alternatives=["Chicken"],
                    rationale="The meat appears red.",
                ),
            )
        replacement = await repository.provision_account(
            rejected_owner_id,
            verified_email_normalized=f"{rejected_owner_id}@example.test",
        )
        assert replacement.owner_user_id == rejected_owner_id
        assert (
            await repository.consent_preferences(firebase_uid=rejected_owner_id)
        ).waitlist_status == "fulfilled"
        identity = await client.collection("identities").document(rejected_owner_id).get()
        assert identity.get("admission_email_verified") is True
        assert (
            identity.get("admission_email_normalized")
            == f"{rejected_owner_id}@example.test"
        )
        with pytest.raises(AccountCapacityStateConflict):
            await repository.change_public_account_capacity(
                account_id=admitted[0].id,
                action=AccountCapacityAction.RESTORE,
                reason=AccountCapacityReason.OPERATOR_REVERSAL,
                operation_id="22222222-2222-4222-8222-222222222222",
            )
        await repository.change_public_account_capacity(
            account_id=admitted[1].id,
            action=AccountCapacityAction.RECLAIM,
            reason=AccountCapacityReason.MISSING_FIREBASE_IDENTITY,
            operation_id="33333333-3333-4333-8333-333333333333",
        )
        restored = await repository.change_public_account_capacity(
            account_id=admitted[0].id,
            action=AccountCapacityAction.RESTORE,
            reason=AccountCapacityReason.OPERATOR_REVERSAL,
            operation_id="22222222-2222-4222-8222-222222222222",
        )
        assert restored.active_public_account_count == 4

        capacity = await client.collection("system").document("public_capacity").get()
        assert capacity.to_dict() == {
            "schema_version": 1,
            "active_account_count": 4,
            "account_limit": 4,
            "waitlist_open": True,
            "updated_at": capacity.get("updated_at"),
        }
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_reclaim_racing_with_admission_preserves_exact_capacity() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-capacity-race-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=1,
            trial_image_limit=200,
            client=client,
        )
        original = await repository.provision_account(
            "firestore-capacity-race-original",
            verified_email_normalized="firestore-capacity-race-original@example.test",
        )

        reclaim, admission = await asyncio.gather(
            repository.change_public_account_capacity(
                account_id=original.id,
                action=AccountCapacityAction.RECLAIM,
                reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
                operation_id="44444444-4444-4444-8444-444444444444",
            ),
            repository.provision_account(
                "firestore-capacity-race-replacement",
                verified_email_normalized="firestore-capacity-race-replacement@example.test",
            ),
            return_exceptions=True,
        )

        assert not isinstance(reclaim, Exception)
        if isinstance(admission, Exception):
            assert isinstance(admission, AccountCapacityReached)
            admission = await repository.provision_account(
                "firestore-capacity-race-replacement",
                verified_email_normalized="firestore-capacity-race-replacement@example.test",
            )
        assert admission.owner_user_id == "firestore-capacity-race-replacement"
        capacity = await client.collection("system").document("public_capacity").get()
        assert capacity.get("active_account_count") == 1
        assert capacity.get("account_limit") == 1
        assert capacity.get("waitlist_open") is True

        malformed = {
            "schema_version": 1,
            "active_account_count": 2,
            "account_limit": 1,
            "waitlist_open": True,
        }
        await capacity.reference.set(malformed)
        with pytest.raises(ValueError, match="count_exceeds_limit"):
            await repository.change_public_account_capacity(
                account_id=admission.id,
                action=AccountCapacityAction.RECLAIM,
                reason=AccountCapacityReason.CONFIRMED_SYBIL_ABUSE,
                operation_id="55555555-5555-4555-8555-555555555555",
            )
        assert (await capacity.reference.get()).to_dict() == malformed
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_journal_uses_occurrence_time_when_publication_is_delayed() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-journal-order-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner_user_id = "journal-order-owner"
        account = await repository.provision_account(owner_user_id)
        camera = await repository.create_browser_camera(
            owner_user_id,
            "Kitchen",
            "journal-order-browser-0001",
        )
        captures = []
        for suffix in ("older", "newer"):
            capture, _, created = await repository.reserve_capture(
                capture_id=f"journal-order-capture-{suffix}",
                account=account,
                camera=camera,
                idempotency_key=f"journal-order-idempotency-{suffix}",
                content_type="image/jpeg",
                content_sha256=sha256(suffix.encode()).hexdigest(),
                object_key=(f"accounts/{account.id}/captures/journal-order-capture-{suffix}.jpg"),
            )
            assert created is True
            captures.append(capture)

        base = utc_now()
        shared = {
            "account_id": account.id,
            "confidence": Confidence.UNCERTAIN,
            "components": [],
            "observations": ["Journal ordering fixture."],
            "alternatives": [],
            "rationale": "Only occurrence-time ordering is under test.",
        }
        older = MealEntry(
            **shared,
            id="journal-order-meal-older",
            capture_id=captures[0].id,
            title="Older occurrence published last",
            occurred_at=base,
            created_at=base + timedelta(hours=2),
        )
        newer = MealEntry(
            **shared,
            id="journal-order-meal-newer",
            capture_id=captures[1].id,
            title="Newer occurrence published first",
            occurred_at=base + timedelta(hours=1),
            created_at=base + timedelta(hours=1, minutes=30),
        )
        await repository.save_meal(account_id=account.id, meal=older)
        await repository.save_meal(account_id=account.id, meal=newer)

        journal = await repository.list_meals(owner_user_id)

        assert [item.id for item in journal] == [newer.id, older.id]
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_user_context_notes_preserve_history_and_tenant_scope() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-context-note-contract-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner = await repository.provision_account("context-contract-owner")
        await repository.provision_account("context-contract-foreign")
        now = utc_now()
        request = UserContextNoteCreate(
            text="My MIL brought duck; we intend to cook it tomorrow.",
            valid_from=now - timedelta(hours=1),
            valid_until=now + timedelta(days=2),
        )
        note = await repository.create_user_context_note(
            owner_user_id="context-contract-owner",
            request=request,
            idempotency_key="context-contract-create-0001",
        )
        assert (
            await repository.create_user_context_note(
                owner_user_id="context-contract-owner",
                request=request,
                idempotency_key="context-contract-create-0001",
            )
            == note
        )
        with pytest.raises(IdempotencyConflict):
            await repository.create_user_context_note(
                owner_user_id="context-contract-owner",
                request=UserContextNoteCreate(text="Different statement"),
                idempotency_key="context-contract-create-0001",
            )
        assert await repository.list_user_context_notes("context-contract-owner") == [note]
        assert await repository.list_user_context_notes("context-contract-foreign") == []
        assert await repository.active_user_context_notes_for_account(
            account_id=owner.id,
            active_at=now,
        ) == [note]
        with pytest.raises(UserContextNoteNotFound):
            await repository.retire_user_context_note(
                owner_user_id="context-contract-foreign",
                note_id=note.id,
            )
        retired = await repository.retire_user_context_note(
            owner_user_id="context-contract-owner",
            note_id=note.id,
        )
        assert retired.status == UserContextNoteStatus.RETIRED
        assert await repository.list_user_context_notes("context-contract-owner") == []
        assert (
            await repository.active_user_context_notes_for_account(
                account_id=owner.id,
                active_at=now,
            )
            == []
        )
        assert await repository.list_user_context_notes(
            "context-contract-owner",
            include_inactive=True,
        ) == [retired]
        snapshot = (
            await client.collection("accounts")
            .document(owner.id)
            .collection("user_context_notes")
            .document(note.id)
            .get()
        )
        raw = snapshot.to_dict() or {}
        assert raw["text"] == request.text
        assert raw["author_user_id"] == "context-contract-owner"
        assert "idempotency_key" not in raw
        assert raw["request_hash"] == sha256(request.model_dump_json().encode()).hexdigest()
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_ai_trace_index_is_immutable_and_tenant_scoped() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-trace-contract-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner = await repository.provision_account("trace-contract-owner")
        foreign = await repository.provision_account("trace-contract-foreign")
        started_at = utc_now()
        trace_id = f"trace-{'a' * 64}"
        trace = AiTraceRecord(
            id=trace_id,
            account_id=owner.id,
            event_id="trace-contract-event",
            reservation_id=f"model-{'b' * 64}",
            root_trace_id=trace_id,
            object_key=f"accounts/{owner.id}/traces/{trace_id}.json.gz",
            content_sha256="c" * 64,
            compressed_size=321,
            status="succeeded",
            model="gemini-3.6-flash",
            model_version="gemini-3.6-flash-001",
            provider_invocation_id="trace-contract-provider-invocation",
            region="eu",
            prompt_version="food-event-v5",
            purpose="event_inference",
            retry_attempt=0,
            evaluation=False,
            prompt_tokens=100,
            response_tokens=20,
            thinking_tokens=5,
            total_tokens=125,
            actual_dkk_micros=1_000,
            latency_ms=500,
            started_at=started_at,
            completed_at=started_at + timedelta(milliseconds=500),
            created_at=started_at + timedelta(milliseconds=500),
        )

        assert await repository.record_ai_trace(trace) == trace
        assert await repository.record_ai_trace(trace) == trace
        assert (
            await repository.ai_trace_for_account(
                account_id=owner.id,
                trace_id=trace.id,
            )
            == trace
        )
        with pytest.raises(AiTraceNotFound):
            await repository.ai_trace_for_account(
                account_id=foreign.id,
                trace_id=trace.id,
            )
        with pytest.raises(AiTraceConflict):
            await repository.record_ai_trace(
                trace.model_copy(update={"compressed_size": trace.compressed_size + 1})
            )
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_meal_question_feedback_and_revisions_are_atomic() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-repository-contract-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner_user_id = "firestore-contract-owner"
        account = await repository.provision_account(owner_user_id)
        capture_id = "firestore-contract-capture"
        await (
            client.collection("accounts")
            .document(account.id)
            .collection("captures")
            .document(capture_id)
            .set(
                {
                    "schema_version": 1,
                    "id": capture_id,
                    "account_id": account.id,
                    "status": "stored",
                }
            )
        )
        meal = MealEntry(
            id="firestore-contract-meal",
            account_id=account.id,
            capture_id=capture_id,
            title="Likely air-fried steak",
            confidence=Confidence.UNCERTAIN,
            components=[
                MealComponent(
                    name="Red meat",
                    ingredients=["red meat"],
                    preparation_methods=["air frying"],
                )
            ],
            observations=["Red meat is visible beside an air-fryer basket."],
            alternatives=["Air-fried lamb"],
            rationale="The meat appears red, but the distant view does not show the cut.",
            clarification_question="Was this steak or lamb?",
            clarification_reason="The answer distinguishes the supported red-meat options.",
        )

        stored = await repository.save_meal(account_id=account.id, meal=meal)
        retry = await repository.save_meal(account_id=account.id, meal=meal)
        assert retry == stored == meal

        questions = await asyncio.gather(
            *(
                repository.open_question(
                    account_id=account.id,
                    meal=meal,
                    prompt="Was this steak or lamb?",
                    reason="The distant view supports both options.",
                )
                for _ in range(2)
            )
        )
        assert questions[0] == questions[1]

        confirmation_request = MealFeedbackRequest(kind=MealFeedbackKind.CONFIRM)
        confirmation = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=confirmation_request,
            idempotency_key="firestore-contract-confirm",
        )
        confirmation_retry = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=confirmation_request,
            idempotency_key="firestore-contract-confirm",
        )
        assert confirmation_retry == confirmation
        assert confirmation.revision.number == 2
        assert confirmation.revision.status == MealStatus.CONFIRMED

        outcomes = await asyncio.gather(
            repository.answer_question(
                owner_user_id=owner_user_id,
                question_id=questions[0].id,
                request=QuestionAnswerRequest(
                    answer="Steak",
                    learning_tip="The beef packet has a dark green label.",
                ),
                idempotency_key="firestore-contract-answer-steak",
            ),
            repository.answer_question(
                owner_user_id=owner_user_id,
                question_id=questions[0].id,
                request=QuestionAnswerRequest(answer="Lamb"),
                idempotency_key="firestore-contract-answer-lamb",
            ),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if isinstance(item, QuestionAnswerResult)]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        assert len(successes) == 1, repr(outcomes)
        assert len(failures) == 1, repr(outcomes)
        assert isinstance(failures[0], QuestionAlreadyAnswered)
        assert successes[0].revision.number == 3

        current = await repository.meal_for_owner(owner_user_id, meal.id)
        revisions = await repository.list_meal_revisions(owner_user_id, meal.id)
        answered = await repository.list_questions(
            owner_user_id,
            question_status=QuestionStatus.ANSWERED,
        )
        feedback = [
            snapshot
            async for snapshot in client.collection("accounts")
            .document(account.id)
            .collection("feedback")
            .stream()
        ]
        assert current.revision_number == 3
        assert [revision.number for revision in revisions] == [1, 2, 3]
        assert [revision.source for revision in revisions] == [
            "inference",
            "user_feedback",
            "user_feedback",
        ]
        assert len(answered) == 1
        assert answered[0].answer == successes[0].question.answer
        assert len(feedback) == 2
        assert all("idempotency_key" not in snapshot.to_dict() for snapshot in feedback)
        confirmation_ref = (
            client.collection("accounts")
            .document(account.id)
            .collection("feedback")
            .document(sha256(b"firestore-contract-confirm").hexdigest())
        )
        confirmation_snapshot = await confirmation_ref.get()
        confirmation_raw = confirmation_snapshot.to_dict() or {}
        assert confirmation_raw["actual_meal"] is None
        assert confirmation_raw["explanation"] is None
        assert confirmation_raw["question_id"] is None
        assert confirmation_raw["idempotency_hash"] == confirmation_snapshot.id

        answer_ref = (
            client.collection("accounts")
            .document(account.id)
            .collection("feedback")
            .document(successes[0].feedback.id)
        )
        answer_raw = (await answer_ref.get()).to_dict() or {}
        assert answer_raw["actual_meal"] == successes[0].question.answer
        assert answer_raw["explanation"] == successes[0].question.learning_tip
        assert answer_raw["question_id"] == successes[0].question.id

        confirmation_retry_again = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=confirmation_request,
            idempotency_key="firestore-contract-confirm",
        )
        assert confirmation_retry_again == confirmation
        assert (await confirmation_ref.get()).to_dict() == confirmation_raw
        with pytest.raises(IdempotencyConflict):
            await repository.record_meal_feedback(
                owner_user_id=owner_user_id,
                meal_id=meal.id,
                request=MealFeedbackRequest(
                    kind=MealFeedbackKind.CORRECT,
                    actual_meal="Different payload",
                ),
                idempotency_key="firestore-contract-confirm",
            )

        targeted_request = MealFeedbackRequest.model_validate(
            {
                "kind": "correct",
                "base_revision_number": 3,
                "correction": {
                    "scope": "component",
                    "component_index": 0,
                    "replacement": {
                        "name": "Ribeye steak",
                        "ingredients": ["beef ribeye"],
                        "preparation_methods": ["air frying"],
                    },
                },
                "explanation": "The package showed ribeye.",
            }
        )
        targeted = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=targeted_request,
            idempotency_key="firestore-contract-targeted",
        )
        targeted_retry = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=targeted_request,
            idempotency_key="firestore-contract-targeted",
        )
        assert targeted_retry == targeted
        assert targeted.revision.number == 4
        assert targeted.revision.base_revision_number == 3
        assert targeted.revision.correction == targeted_request.correction
        assert targeted.revision.inference.components[0].name == "Ribeye steak"

        targeted_ref = (
            client.collection("accounts")
            .document(account.id)
            .collection("feedback")
            .document(sha256(b"firestore-contract-targeted").hexdigest())
        )
        targeted_raw = (await targeted_ref.get()).to_dict() or {}
        assert targeted_raw["base_revision_number"] == 3
        assert targeted_raw["correction"]["scope"] == "component"
        assert "idempotency_key" not in targeted_raw
        current = await repository.meal_for_owner(owner_user_id, meal.id)
        assert current.revision_number == 4
        assert current.components[0].name == "Ribeye steak"

        with pytest.raises(MealRevisionConflict):
            await repository.record_meal_feedback(
                owner_user_id=owner_user_id,
                meal_id=meal.id,
                request=targeted_request,
                idempotency_key="firestore-contract-stale-targeted",
            )
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_not_cooking_is_hidden_auditable_and_reclassifiable() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-not-cooking-contract-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner_user_id = "firestore-not-cooking-owner"
        account = await repository.provision_account(owner_user_id)
        await repository.provision_account("firestore-not-cooking-foreign")
        capture_id = "firestore-not-cooking-capture"
        await (
            client.collection("accounts")
            .document(account.id)
            .collection("captures")
            .document(capture_id)
            .set(
                {
                    "schema_version": 1,
                    "id": capture_id,
                    "account_id": account.id,
                    "status": "stored",
                }
            )
        )
        meal = await repository.save_meal(
            account_id=account.id,
            meal=MealEntry(
                id="firestore-not-cooking-meal",
                account_id=account.id,
                capture_id=capture_id,
                title="Cat on the counter",
                confidence=Confidence.UNCERTAIN,
                components=[],
                observations=["A cat is standing by the sink."],
                alternatives=["Food preparation"],
                rationale="No food is visible.",
                clarification_question="Was this only the cat, rather than cooking?",
                clarification_reason="Only cooking belongs in the food journal.",
            ),
        )
        question = await repository.open_question(
            account_id=account.id,
            meal=meal,
            prompt="Was this the cat or food preparation?",
            reason="Only food preparation belongs in the journal.",
        )
        confirmation = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=MealFeedbackRequest(kind=MealFeedbackKind.CONFIRM),
            idempotency_key="firestore-not-cooking-confirm-before-discard",
        )
        assert confirmation.revision.number == 2
        request = MealFeedbackRequest(
            kind=MealFeedbackKind.NOT_COOKING,
            explanation="The cat jumped onto the counter; nobody was preparing food.",
        )

        discarded = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=request,
            idempotency_key="firestore-not-cooking-discard",
        )
        retry = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=request,
            idempotency_key="firestore-not-cooking-discard",
        )

        assert retry == discarded
        assert discarded.revision.status == MealStatus.NOT_COOKING
        assert await repository.list_meals(owner_user_id) == []
        retained = await repository.meal_for_owner(owner_user_id, meal.id)
        assert retained.status == MealStatus.NOT_COOKING
        revisions = await repository.list_meal_revisions(owner_user_id, meal.id)
        assert [revision.status for revision in revisions] == [
            MealStatus.PROVISIONAL,
            MealStatus.CONFIRMED,
            MealStatus.NOT_COOKING,
        ]
        superseded = await repository.list_questions(
            owner_user_id,
            question_status=QuestionStatus.SUPERSEDED,
        )
        assert [item.id for item in superseded] == [question.id]
        with pytest.raises(InvalidMealFeedbackTransition):
            await repository.record_meal_feedback(
                owner_user_id=owner_user_id,
                meal_id=meal.id,
                request=MealFeedbackRequest(kind=MealFeedbackKind.NOT_COOKING),
                idempotency_key="firestore-not-cooking-discard-again",
            )
        with pytest.raises(MealNotFound):
            await repository.record_meal_feedback(
                owner_user_id="firestore-not-cooking-foreign",
                meal_id=meal.id,
                request=MealFeedbackRequest(kind=MealFeedbackKind.NOT_COOKING),
                idempotency_key="firestore-not-cooking-foreign-discard",
            )

        restored = await repository.record_meal_feedback(
            owner_user_id=owner_user_id,
            meal_id=meal.id,
            request=MealFeedbackRequest(
                kind=MealFeedbackKind.CORRECT,
                actual_meal="Steak",
                explanation="I discarded the wrong event; this was cooking.",
            ),
            idempotency_key="firestore-not-cooking-restore",
        )
        assert restored.revision.status == MealStatus.CORRECTED
        assert restored.revision.number == 4
        assert [item.id for item in await repository.list_meals(owner_user_id)] == [meal.id]
        final_revisions = await repository.list_meal_revisions(owner_user_id, meal.id)
        assert [revision.status for revision in final_revisions] == [
            MealStatus.PROVISIONAL,
            MealStatus.CONFIRMED,
            MealStatus.NOT_COOKING,
            MealStatus.CORRECTED,
        ]
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_knowledge_revisions_are_atomic_provenanced_and_tenant_scoped() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-knowledge-contract-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner = await repository.provision_account("knowledge-contract-owner")
        foreign = await repository.provision_account("knowledge-contract-foreign-owner")
        initial_draft = KnowledgeRevisionDraft(
            title="Thursday dinner pattern",
            statement="Steak may often be eaten on Thursdays.",
            claim=KnowledgeClaim(
                dimension="likely meal",
                value="steak",
                conditions=("thursday",),
            ),
            lifecycle=KnowledgeLifecycle.INFERRED,
            belief_strength=KnowledgeBeliefStrength.WEAK,
            source=KnowledgeRevisionSource.AGENT_INFERENCE,
            evidence=[
                KnowledgeEvidenceReference(
                    kind=KnowledgeEvidenceKind.MEAL_REVISION,
                    id="knowledge-contract-meal-001",
                    role=KnowledgeEvidenceRole.SUPPORTS,
                )
            ],
            reason="One observed Thursday meal supports a tentative pattern.",
        )
        initial = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key="Thursday dinner pattern",
            expected_revision_number=None,
            draft=initial_draft,
            idempotency_key="knowledge-contract-initial",
        )
        initial_retry = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key="  thursday   dinner pattern ",
            expected_revision_number=None,
            draft=initial_draft,
            idempotency_key="knowledge-contract-initial",
        )
        assert initial_retry == initial

        drafts = [
            KnowledgeRevisionDraft(
                title="Thursday dinner pattern",
                statement=statement,
                claim=KnowledgeClaim(
                    dimension="likely meal",
                    value="steak" if index == 0 else "red meat",
                    conditions=("thursday",),
                ),
                lifecycle=KnowledgeLifecycle.REINFORCED,
                belief_strength=KnowledgeBeliefStrength.MODERATE,
                source=KnowledgeRevisionSource.AGENT_INFERENCE,
                evidence=[
                    KnowledgeEvidenceReference(
                        kind=KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                        id=initial.revision.id,
                        role=KnowledgeEvidenceRole.CONTEXT,
                    ),
                    KnowledgeEvidenceReference(
                        kind=KnowledgeEvidenceKind.MEAL_REVISION,
                        id=f"knowledge-contract-meal-00{index + 2}",
                        role=KnowledgeEvidenceRole.SUPPORTS,
                    ),
                ],
                reason="A second observed meal reinforces the tentative pattern.",
            )
            for index, statement in enumerate(
                [
                    "Steak is often eaten on Thursdays.",
                    "Red meat is often eaten on Thursdays.",
                ]
            )
        ]
        outcomes = await asyncio.gather(
            *(
                repository.record_knowledge_revision(
                    account_id=owner.id,
                    topic_key=initial.page.topic_key,
                    expected_revision_number=1,
                    draft=candidate,
                    idempotency_key=f"knowledge-contract-competing-{index}",
                )
                for index, candidate in enumerate(drafts)
            ),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if isinstance(item, KnowledgeRevisionResult)]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        assert len(successes) == 1, repr(outcomes)
        assert len(failures) == 1, repr(outcomes)
        assert isinstance(failures[0], KnowledgeRevisionConflict)
        winner_index = outcomes.index(successes[0])
        winner = successes[0]
        winner_retry = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key=initial.page.topic_key,
            expected_revision_number=1,
            draft=drafts[winner_index],
            idempotency_key=f"knowledge-contract-competing-{winner_index}",
        )
        assert winner_retry == winner
        request_result = await repository.knowledge_revision_result_for_request(
            account_id=owner.id,
            idempotency_key=f"knowledge-contract-competing-{winner_index}",
        )
        current_result = await repository.current_knowledge_revision(
            account_id=owner.id,
            topic_key=initial.page.topic_key,
        )
        assert request_result == winner
        assert current_result == winner

        page = await repository.knowledge_page_for_owner(
            "knowledge-contract-owner", initial.page.id
        )
        revisions = await repository.list_knowledge_revisions(
            "knowledge-contract-owner", initial.page.id
        )
        assert page.current_revision_number == 2
        assert page.current_revision_id == winner.revision.id
        assert [revision.number for revision in revisions] == [1, 2]
        assert revisions[0].statement == initial_draft.statement
        assert page.claim == winner.revision.claim
        assert revisions[0].claim == initial_draft.claim
        assert revisions[1].previous_revision_id == revisions[0].id
        assert any(
            item.kind == KnowledgeEvidenceKind.KNOWLEDGE_REVISION and item.id == revisions[0].id
            for item in revisions[1].evidence
        )
        assert await repository.knowledge_page_index_for_account(account_id=owner.id) == [
            winner.page
        ]
        assert (
            await repository.active_knowledge_revision_for_account(
                account_id=owner.id,
                page_id=winner.page.id,
            )
            == winner
        )
        assert await repository.knowledge_page_index_for_account(account_id=foreign.id) == []

        account_ref = client.collection("accounts").document(owner.id)
        request_documents = [
            snapshot
            async for snapshot in account_ref.collection("knowledge_revision_requests").stream()
        ]
        assert len(request_documents) == 2
        assert all("idempotency_key" not in item.to_dict() for item in request_documents)
        with pytest.raises(KnowledgePageNotFound):
            await repository.knowledge_page_for_owner(
                "knowledge-contract-foreign-owner", initial.page.id
            )
        with pytest.raises(KnowledgePageNotFound):
            await repository.active_knowledge_revision_for_account(
                account_id=foreign.id,
                page_id=winner.page.id,
            )
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_pattern_question_lifecycle_is_atomic_and_tenant_scoped() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-pattern-question-contract-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner = await repository.provision_account("pattern-owner")
        await repository.provision_account("pattern-foreign-owner")
        evidence = [
            QuestionEvidenceReference(
                kind=QuestionEvidenceKind.MEAL_REVISION,
                id="pattern-meal-revision-001",
            )
        ]
        first = await repository.open_pattern_question(
            account_id=owner.id,
            prompt="Steak appears most Thursdays. Is that accurate?",
            reason="Three Thursday meal revisions support the tentative pattern.",
            tentative_claim="Steak is usually eaten on Thursdays",
            evidence=evidence,
        )
        duplicate = await repository.open_pattern_question(
            account_id=owner.id,
            prompt="A differently worded duplicate should not create a record.",
            reason="The same normalized claim is already durable.",
            tentative_claim="  steak is usually eaten on THURSDAYS ",
            evidence=evidence,
        )
        assert duplicate.id == first.id

        replacement = await repository.open_pattern_question(
            account_id=owner.id,
            prompt="Steak appears late in the work week. Is that accurate?",
            reason="New evidence narrows the earlier pattern.",
            tentative_claim="Steak is usually eaten late in the work week",
            evidence=evidence,
            supersedes_question_id=first.id,
        )
        all_questions = await repository.list_questions(
            "pattern-owner",
            question_status=None,
        )
        old = next(question for question in all_questions if question.id == first.id)
        assert old.status == QuestionStatus.SUPERSEDED
        assert old.superseded_by_question_id == replacement.id
        with pytest.raises(QuestionSuperseded):
            await repository.respond_to_question(
                owner_user_id="pattern-owner",
                question_id=first.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.REJECT),
                idempotency_key="pattern-stale-response",
            )
        with pytest.raises(QuestionNotFound):
            await repository.respond_to_question(
                owner_user_id="pattern-foreign-owner",
                question_id=replacement.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.CONFIRM),
                idempotency_key="pattern-foreign-response",
            )

        request = QuestionResponseRequest(
            kind=QuestionResponseKind.CORRECT,
            correction="Steak is common near the end of the work week.",
            explanation="Thursday is not strict when plans move.",
        )
        result = await repository.respond_to_question(
            owner_user_id="pattern-owner",
            question_id=replacement.id,
            request=request,
            idempotency_key="pattern-correct-response",
        )
        retry = await repository.respond_to_question(
            owner_user_id="pattern-owner",
            question_id=replacement.id,
            request=request,
            idempotency_key="pattern-correct-response",
        )
        assert retry == result
        assert result.feedback is None
        assert result.revision is None
        assert result.question.response_kind == QuestionResponseKind.CORRECT
        response_snapshot = await (
            client.collection("accounts")
            .document(owner.id)
            .collection("question_responses")
            .document(result.response.id)
            .get()
        )
        response_raw = response_snapshot.to_dict() or {}
        assert "idempotency_key" not in response_raw
        assert response_raw["idempotency_hash"] == result.response.id
        assert response_raw["correction"] == request.correction
        with pytest.raises(IdempotencyConflict):
            await repository.respond_to_question(
                owner_user_id="pattern-owner",
                question_id=replacement.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.REJECT),
                idempotency_key="pattern-correct-response",
            )

        competing = await repository.open_pattern_question(
            account_id=owner.id,
            prompt="Weekday breakfasts appear different from weekends. Is that accurate?",
            reason="The cited meal revisions show two candidate routines.",
            tentative_claim="Weekday and weekend breakfasts follow different routines",
            evidence=evidence,
        )
        outcomes = await asyncio.gather(
            repository.respond_to_question(
                owner_user_id="pattern-owner",
                question_id=competing.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.CONFIRM),
                idempotency_key="pattern-race-confirm",
            ),
            repository.respond_to_question(
                owner_user_id="pattern-owner",
                question_id=competing.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.REJECT),
                idempotency_key="pattern-race-reject",
            ),
            return_exceptions=True,
        )
        assert len([item for item in outcomes if isinstance(item, QuestionResponseResult)]) == 1, (
            repr(outcomes)
        )
        failures = [item for item in outcomes if isinstance(item, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], QuestionAlreadyAnswered)
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_event_publication_atomically_fences_lease_and_revision() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-event-publication-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        account = await repository.provision_account("event-publication-owner")
        now = utc_now()
        event = ActivityEvent(
            id="event-publication-event",
            account_id=account.id,
            camera_ids=["event-publication-camera"],
            first_capture_at=now,
            last_capture_at=now,
            capture_count=1,
            grouping_policy_version="publication-test-v1",
        )
        capture = CaptureRecord(
            id="event-publication-capture",
            account_id=account.id,
            camera_id=event.camera_ids[0],
            idempotency_key="event-publication-idempotency",
            content_type="image/jpeg",
            content_sha256="a" * 64,
            object_key=(f"accounts/{account.id}/captures/event-publication-capture.jpg"),
            event_id=event.id,
            status=CaptureStatus.STORED,
            created_at=now,
        )
        job = DurableJob(
            id=event_inference_job_id(event.id),
            account_id=account.id,
            kind=JobKind.EVENT_INFERENCE,
            subject_id=event.id,
            subject_revision=event.current_revision,
            status=JobStatus.LEASED,
            attempt_count=1,
            lease_id="event-publication-lease",
            lease_owner="event-publication-worker",
            lease_expires_at=now + timedelta(minutes=5),
            created_at=now,
        )
        account_ref = client.collection("accounts").document(account.id)
        capture_data = capture.model_dump(mode="python", exclude={"idempotency_key"})
        capture_data.update(schema_version=1, idempotency_hash="test-only")
        await (
            account_ref.collection("events")
            .document(event.id)
            .set({**event.model_dump(mode="python"), "schema_version": 1})
        )
        await account_ref.collection("captures").document(capture.id).set(capture_data)
        await (
            account_ref.collection("jobs")
            .document(job.id)
            .set({**job.model_dump(mode="python"), "schema_version": 1})
        )

        payload = base_payload()
        payload["event_id"] = event.id
        payload["source_capture_ids"] = [capture.id]
        payload["direct_observations"][0]["image_evidence"][0]["capture_id"] = capture.id
        hypothesis = ActivityMealInferenceV1.model_validate(payload)
        meal = await repository.publish_event_inference(
            account_id=account.id,
            event_id=event.id,
            expected_event_revision=event.current_revision,
            lease_id=job.lease_id or "",
            lease_owner=job.lease_owner or "",
            hypothesis=hypothesis,
        )
        assert meal is not None
        assert meal.activity_hypothesis == hypothesis

        duplicate = await repository.publish_event_inference(
            account_id=account.id,
            event_id=event.id,
            expected_event_revision=event.current_revision,
            lease_id=job.lease_id or "",
            lease_owner=job.lease_owner or "",
            hypothesis=hypothesis,
        )
        assert duplicate is None
        stored_event = await account_ref.collection("events").document(event.id).get()
        stored_job = await account_ref.collection("jobs").document(job.id).get()
        stored_capture = await account_ref.collection("captures").document(capture.id).get()
        questions = await repository.list_questions(
            "event-publication-owner",
            question_status=QuestionStatus.OPEN,
        )
        revisions = [
            snapshot
            async for snapshot in account_ref.collection("meals")
            .document(meal.id)
            .collection("revisions")
            .stream()
        ]
        assert stored_event.get("status") == ActivityEventStatus.INFERRED
        assert stored_event.get("meal_id") == meal.id
        assert stored_job.get("status") == JobStatus.COMPLETED
        assert stored_capture.get("status") == CaptureStatus.PROCESSED
        assert len(revisions) == 1
        assert len(questions) == 1
        assert questions[0].kind == "event_clarification"
        assert questions[0].meal_id == meal.id
        assert questions[0].event_id == event.id
        assert questions[0].choices == ["Air-fried steak", "Air-fried lamb"]
        assert questions[0].source_revision_number == 1
        assert await repository.recent_meals_for_account(account_id=account.id) == [meal]
        unresolved_meals, unresolved_questions = await repository.unresolved_reviews_for_account(
            account_id=account.id
        )
        assert unresolved_meals == [meal]
        assert unresolved_questions == questions
        client.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_deployed_repository_smoke_is_rerunnable_without_duplicate_revisions() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-repository-smoke-test"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        await ensure_smoke_fixture(client)
        await ensure_smoke_fixture(client)

        first = await run_smoke(repository)
        second = await run_smoke(repository)

        assert first == second
        assert first["revision_numbers"] == [1, 2, 3, 4, 5, 6, 7]
        assert first["knowledge_revision_numbers"] == [1, 2]
        assert first["knowledge_lifecycle"] == "reinforced"
        assert first["proposed_knowledge_revision_numbers"] == [1, 2]
        assert first["proposed_knowledge_lifecycle"] == "confirmed"
        assert first["feedback_knowledge_revision"] == 1
        assert first["knowledge_index_count"] == 3
        assert set(first["knowledge_index_page_ids"]) == {
            first["knowledge_page_id"],
            first["proposed_knowledge_page_id"],
            first["feedback_knowledge_page_id"],
        }
        assert first["selected_knowledge_revision_number"] == 2
        assert first["selected_knowledge_revision_id"]
        assert first["not_cooking_revision"] == 6
        assert first["reclassified_revision"] == 7
        assert first["model_calls"] == 0
        feedback = [
            snapshot
            async for snapshot in client.collection("accounts")
            .document(SMOKE_ACCOUNT_ID)
            .collection("feedback")
            .stream()
        ]
        assert len(feedback) == 6
        client.close()

    asyncio.run(scenario())
