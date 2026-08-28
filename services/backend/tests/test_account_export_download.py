import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from foodlog_backend.account_exports import EXPORT_CONTENT_TYPE, export_archive_object_key
from foodlog_backend.app import create_app
from foodlog_backend.http_ranges import (
    MAX_HTTP1_FIXED_LENGTH_RESPONSE_BYTES,
    RangeNotSatisfiable,
    fixed_content_length,
    parse_single_byte_range,
)
from foodlog_backend.models import AccountExport, AuditAction
from foodlog_backend.settings import Settings


async def create_export(
    app,
    *,
    owner_user_id: str,
    content: bytes | None,
    requested_at: datetime | None = None,
    completed_at: datetime | None = None,
    expires_at: datetime | None = None,
    declared_size: int | None = None,
    fail: bool = False,
) -> AccountExport:
    repository = app.state.container.repository
    export_store = app.state.container.export_object_store
    account = await repository.provision_account(owner_user_id)
    admitted_at = requested_at or datetime.now(UTC) - timedelta(seconds=1)
    account_export, created = await repository.create_account_export(
        owner_user_id=owner_user_id,
        idempotency_key=f"download-{owner_user_id}-0001",
        requested_at=admitted_at,
        cooldown=timedelta(hours=1),
    )
    assert created is True
    lease_id = f"lease-{owner_user_id}"
    claimed = await repository.claim_account_export(
        account_id=account.id,
        export_id=account_export.id,
        lease_id=lease_id,
        lease_owner="download-test-worker",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    assert claimed is not None
    if fail:
        assert await repository.fail_account_export(
            account_id=account.id,
            export_id=account_export.id,
            lease_id=lease_id,
            error_code="TestFailure",
            failed_at=datetime.now(UTC),
        )
    elif content is not None:
        object_key = export_archive_object_key(account.id, account_export.id)
        await export_store.put(
            account.id,
            object_key,
            content,
            EXPORT_CONTENT_TYPE,
        )
        finished_at = completed_at or datetime.now(UTC)
        completed = await repository.complete_account_export(
            account_id=account.id,
            export_id=account_export.id,
            lease_id=lease_id,
            archive_object_key=object_key,
            archive_size=declared_size or len(content),
            archive_sha256=sha256(content).hexdigest(),
            manifest_sha256="a" * 64,
            completed_at=finished_at,
            expires_at=expires_at or finished_at + timedelta(hours=24),
        )
        assert completed is not None
    return account_export


@pytest.mark.parametrize(
    ("value", "total", "expected"),
    [
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=10-", 100, (10, 99)),
        ("bytes=-10", 100, (90, 99)),
        ("bytes=-1000", 100, (0, 99)),
        ("bytes=90-1000", 100, (90, 99)),
    ],
)
def test_single_range_parser(value: str, total: int, expected: tuple[int, int]) -> None:
    parsed = parse_single_byte_range(value, total=total)
    assert (parsed.start, parsed.end) == expected


@pytest.mark.parametrize(
    "value",
    ["bytes=-", "bytes=100-", "bytes=10-9", "bytes=0-1,4-5", "items=0-1"],
)
def test_single_range_parser_rejects_invalid_or_multiple_ranges(value: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_single_byte_range(value, total=100)


def test_fixed_content_length_selects_chunking_beyond_cloud_run_limit() -> None:
    assert fixed_content_length(MAX_HTTP1_FIXED_LENGTH_RESPONSE_BYTES) == str(
        MAX_HTTP1_FIXED_LENGTH_RESPONSE_BYTES
    )
    assert fixed_content_length(MAX_HTTP1_FIXED_LENGTH_RESPONSE_BYTES + 1) is None
    with pytest.raises(ValueError):
        fixed_content_length(0)


def test_owner_can_stream_full_and_resumable_private_export() -> None:
    app = create_app(Settings(environment="test"))
    content = bytes(range(256)) * 8_193
    account_export = asyncio.run(
        create_export(
            app,
            owner_user_id="download-owner",
            content=content,
        )
    )
    headers = {"X-FoodLog-Local-User": "download-owner"}
    path = f"/v1/exports/{account_export.id}/download"
    with TestClient(app) as client:
        full = client.get(path, headers=headers)
        bounded = client.get(path, headers={**headers, "Range": "bytes=10-19"})
        open_ended = client.get(path, headers={**headers, "Range": "bytes=100-"})
        suffix = client.get(path, headers={**headers, "Range": "bytes=-17"})

    assert full.status_code == 200
    assert full.content == content
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["cache-control"] == "private, no-store"
    assert full.headers["content-type"] == EXPORT_CONTENT_TYPE
    assert full.headers["content-length"] == str(len(content))
    assert full.headers["content-disposition"] == (
        f'attachment; filename="foodlog-export-{account_export.id}.zip"'
    )
    assert "content-range" not in full.headers
    assert bounded.status_code == open_ended.status_code == suffix.status_code == 206
    assert bounded.content == content[10:20]
    assert bounded.headers["content-range"] == f"bytes 10-19/{len(content)}"
    assert open_ended.content == content[100:]
    assert suffix.content == content[-17:]
    audits = asyncio.run(
        app.state.container.repository.list_audit_events_for_owner("download-owner")
    )
    assert [event.action for event in audits].count(
        AuditAction.ACCOUNT_EXPORT_DOWNLOADED
    ) == 1


def test_large_full_download_uses_chunked_streaming() -> None:
    app = create_app(Settings(environment="test"))
    content = b"x" * (MAX_HTTP1_FIXED_LENGTH_RESPONSE_BYTES + 1)
    account_export = asyncio.run(
        create_export(app, owner_user_id="large-download-owner", content=content)
    )
    with TestClient(app) as client:
        response = client.get(
            f"/v1/exports/{account_export.id}/download",
            headers={"X-FoodLog-Local-User": "large-download-owner"},
        )

    assert response.status_code == 200
    assert response.content == content
    assert "content-length" not in response.headers


def test_invalid_range_returns_416_without_streaming_bytes() -> None:
    app = create_app(Settings(environment="test"))
    content = b"valid-private-export"
    account_export = asyncio.run(
        create_export(app, owner_user_id="range-owner", content=content)
    )
    with TestClient(app) as client:
        response = client.get(
            f"/v1/exports/{account_export.id}/download",
            headers={
                "X-FoodLog-Local-User": "range-owner",
                "Range": "bytes=999-1000",
            },
        )
    assert response.status_code == 416
    assert response.content == b""
    assert response.headers["content-range"] == f"bytes */{len(content)}"
    assert response.headers["cache-control"] == "private, no-store"


def test_download_rejects_foreign_pending_failed_and_expired_exports() -> None:
    app = create_app(Settings(environment="test"))
    completed = asyncio.run(
        create_export(app, owner_user_id="completed-owner", content=b"complete")
    )
    asyncio.run(app.state.container.repository.provision_account("foreign-owner"))
    pending = asyncio.run(
        create_export(app, owner_user_id="pending-owner", content=None)
    )
    failed = asyncio.run(
        create_export(app, owner_user_id="failed-owner", content=None, fail=True)
    )
    now = datetime.now(UTC)
    expired = asyncio.run(
        create_export(
            app,
            owner_user_id="expired-owner",
            content=b"expired",
            requested_at=now - timedelta(hours=26),
            completed_at=now - timedelta(hours=25),
            expires_at=now - timedelta(hours=1),
        )
    )
    with TestClient(app) as client:
        foreign_response = client.get(
            f"/v1/exports/{completed.id}/download",
            headers={"X-FoodLog-Local-User": "foreign-owner"},
        )
        pending_response = client.get(
            f"/v1/exports/{pending.id}/download",
            headers={"X-FoodLog-Local-User": "pending-owner"},
        )
        failed_response = client.get(
            f"/v1/exports/{failed.id}/download",
            headers={"X-FoodLog-Local-User": "failed-owner"},
        )
        expired_response = client.get(
            f"/v1/exports/{expired.id}/download",
            headers={"X-FoodLog-Local-User": "expired-owner"},
        )

    assert foreign_response.status_code == 404
    assert foreign_response.content == b""
    assert pending_response.status_code == 409
    assert pending_response.json() == {"detail": "account_export_not_ready"}
    assert failed_response.status_code == 409
    assert failed_response.json() == {"detail": "account_export_failed"}
    assert expired_response.status_code == 410
    assert expired_response.json() == {"detail": "account_export_expired"}


def test_download_fails_closed_when_archive_metadata_does_not_match() -> None:
    app = create_app(Settings(environment="test"))
    account_export = asyncio.run(
        create_export(
            app,
            owner_user_id="mismatch-owner",
            content=b"short",
            declared_size=99,
        )
    )
    with TestClient(app) as client:
        response = client.get(
            f"/v1/exports/{account_export.id}/download",
            headers={"X-FoodLog-Local-User": "mismatch-owner"},
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "account_export_unavailable"}
