import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from foodlog_backend.app import create_app
from foodlog_backend.auth import InvalidAuthenticationToken, VerifiedIdentity
from foodlog_backend.errors import (
    AccountExportAlreadyActive,
    AccountExportNotFound,
    AccountExportRateLimited,
)
from foodlog_backend.models import AccountExport, AuditAction, JobKind, JobStatus
from foodlog_backend.repository import InMemoryRepository
from foodlog_backend.settings import Settings


class ExportTokenVerifier:
    def __init__(self, identities: dict[str, VerifiedIdentity]) -> None:
        self._identities = identities

    async def verify(self, token: str) -> VerifiedIdentity:
        try:
            return self._identities[token]
        except KeyError as error:
            raise InvalidAuthenticationToken from error


def repository() -> InMemoryRepository:
    return InMemoryRepository(public_account_limit=25, trial_image_limit=200)


def test_export_request_atomically_freezes_snapshot_job_and_audit() -> None:
    async def scenario() -> None:
        repo = repository()
        account = await repo.provision_account("export-owner")
        requested_at = datetime(2026, 8, 28, 1, 2, 3, tzinfo=UTC)

        account_export, created = await repo.create_account_export(
            owner_user_id=account.owner_user_id,
            idempotency_key="export-request-0001",
            requested_at=requested_at,
            cooldown=timedelta(hours=1),
        )
        retry, retry_created = await repo.create_account_export(
            owner_user_id=account.owner_user_id,
            idempotency_key="export-request-0001",
            requested_at=requested_at + timedelta(minutes=2),
            cooldown=timedelta(hours=1),
        )
        job = await repo.job_for_account(account.id, account_export.job_id)
        audits = await repo.list_audit_events_for_owner(account.owner_user_id)

        assert created is True
        assert retry_created is False
        assert retry == account_export
        assert account_export.snapshot_at == requested_at
        assert account_export.requested_at == requested_at
        assert account_export.requested_by_user_id == account.owner_user_id
        assert job is not None
        assert job.kind == JobKind.ACCOUNT_EXPORT
        assert job.subject_id == account_export.id
        assert job.status == JobStatus.PENDING
        assert job.available_at == requested_at
        assert [event.action for event in audits] == [
            AuditAction.ACCOUNT_EXPORT_REQUESTED
        ]
        assert audits[0].subject_id == account_export.id
        assert "export-request-0001" not in repr(repo.__dict__)

    asyncio.run(scenario())


def test_pending_account_export_round_trips_explicit_null_timestamps() -> None:
    requested_at = datetime.now(UTC)
    account_export = AccountExport(
        id="a" * 64,
        account_id="account-a",
        requested_by_user_id="owner-a",
        job_id=f"account-export-{'a' * 64}",
        snapshot_at=requested_at,
        requested_at=requested_at,
    )

    restored = AccountExport.model_validate(account_export.model_dump(mode="python"))

    assert restored == account_export


def test_concurrent_export_requests_leave_exactly_one_active_job() -> None:
    async def scenario() -> None:
        repo = repository()
        account = await repo.provision_account("concurrent-export-owner")
        requested_at = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
        results = await asyncio.gather(
            *(
                repo.create_account_export(
                    owner_user_id=account.owner_user_id,
                    idempotency_key=f"concurrent-export-{index:04d}",
                    requested_at=requested_at,
                    cooldown=timedelta(hours=1),
                )
                for index in range(20)
            ),
            return_exceptions=True,
        )
        created = [result for result in results if not isinstance(result, Exception)]
        rejected = [result for result in results if isinstance(result, Exception)]

        assert len(created) == 1
        assert len(rejected) == 19
        assert all(isinstance(error, AccountExportAlreadyActive) for error in rejected)
        assert len(repo._account_exports) == 1
        assert len(
            [job for job in repo._jobs.values() if job.kind == JobKind.ACCOUNT_EXPORT]
        ) == 1

    asyncio.run(scenario())


def test_export_rate_limit_reports_the_exact_remaining_window() -> None:
    async def scenario() -> None:
        repo = repository()
        account = await repo.provision_account("rate-limited-export-owner")
        requested_at = datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
        first, _ = await repo.create_account_export(
            owner_user_id=account.owner_user_id,
            idempotency_key="rate-export-0001",
            requested_at=requested_at,
            cooldown=timedelta(hours=1),
        )

        # OPS-007 owns the completion transition. Simulate its clearing of the
        # active pointer so this foundation's independent cooldown can be tested.
        repo._active_export_by_account.pop(account.id)
        try:
            await repo.create_account_export(
                owner_user_id=account.owner_user_id,
                idempotency_key="rate-export-0002",
                requested_at=requested_at + timedelta(minutes=15),
                cooldown=timedelta(hours=1),
            )
        except AccountExportRateLimited as error:
            assert error.retry_after_seconds == 2_700
        else:
            raise AssertionError("a second export inside the cooldown was accepted")

        retry, created = await repo.create_account_export(
            owner_user_id=account.owner_user_id,
            idempotency_key="rate-export-0001",
            requested_at=requested_at + timedelta(minutes=15),
            cooldown=timedelta(hours=1),
        )
        assert created is False
        assert retry == first

    asyncio.run(scenario())


def test_export_lookup_is_owner_scoped() -> None:
    async def scenario() -> None:
        repo = repository()
        owner = await repo.provision_account("export-owner-a")
        await repo.provision_account("export-owner-b")
        account_export, _ = await repo.create_account_export(
            owner_user_id=owner.owner_user_id,
            idempotency_key="scoped-export-0001",
            requested_at=datetime(2026, 8, 28, 4, 0, tzinfo=UTC),
            cooldown=timedelta(hours=1),
        )

        assert (
            await repo.account_export_for_owner(
                owner_user_id=owner.owner_user_id,
                export_id=account_export.id,
            )
            == account_export
        )
        try:
            await repo.account_export_for_owner(
                owner_user_id="export-owner-b",
                export_id=account_export.id,
            )
        except AccountExportNotFound:
            pass
        else:
            raise AssertionError("another owner could read the export")

    asyncio.run(scenario())


def test_export_api_requires_recent_reauthentication_and_hides_internal_scope() -> None:
    now = datetime.now(UTC)
    verifier = ExportTokenVerifier(
        {
            "old-token": VerifiedIdentity(
                uid="firebase-export-owner",
                email_verified=True,
                email="firebase-export-owner@example.test",
                authenticated_at=now - timedelta(hours=1),
            ),
            "fresh-token": VerifiedIdentity(
                uid="firebase-export-owner",
                email_verified=True,
                email="firebase-export-owner@example.test",
                authenticated_at=now,
            ),
            "other-token": VerifiedIdentity(
                uid="firebase-export-other",
                email_verified=True,
                email="firebase-export-other@example.test",
                authenticated_at=now,
            ),
        }
    )
    app = create_app(
        Settings(
            environment="test",
            auth_backend="firebase",
            firebase_project_id="test-firebase-project",
        ),
        token_verifier=verifier,
    )
    with TestClient(app) as client:
        assert client.post(
            "/v1/accounts", headers={"Authorization": "Bearer old-token"}
        ).status_code == 200
        assert client.post(
            "/v1/accounts", headers={"Authorization": "Bearer other-token"}
        ).status_code == 200
        stale = client.post(
            "/v1/exports",
            headers={
                "Authorization": "Bearer old-token",
                "Idempotency-Key": "api-export-0001",
            },
        )
        created = client.post(
            "/v1/exports",
            headers={
                "Authorization": "Bearer fresh-token",
                "Idempotency-Key": "api-export-0001",
            },
        )
        retry = client.post(
            "/v1/exports",
            headers={
                "Authorization": "Bearer fresh-token",
                "Idempotency-Key": "api-export-0001",
            },
        )
        other_active = client.post(
            "/v1/exports",
            headers={
                "Authorization": "Bearer fresh-token",
                "Idempotency-Key": "api-export-0002",
            },
        )
        owner_read = client.get(
            f"/v1/exports/{created.json()['id']}",
            headers={"Authorization": "Bearer fresh-token"},
        )
        foreign_read = client.get(
            f"/v1/exports/{created.json()['id']}",
            headers={"Authorization": "Bearer other-token"},
        )

    assert stale.status_code == 403
    assert stale.json() == {"detail": "recent_authentication_required"}
    assert created.status_code == 202
    assert retry.status_code == 200
    assert retry.json() == created.json()
    assert created.headers["cache-control"] == "no-store"
    assert set(created.json()) == {
        "schema_version",
        "id",
        "status",
        "snapshot_at",
        "requested_at",
    }
    assert created.json()["snapshot_at"] == created.json()["requested_at"]
    assert other_active.status_code == 409
    assert other_active.json() == {
        "detail": "account_export_already_active",
        "active_export_id": created.json()["id"],
    }
    assert owner_read.status_code == 200
    assert owner_read.headers["cache-control"] == "no-store"
    assert foreign_read.status_code == 404
