from __future__ import annotations

import argparse
import json
import os
import time
from hashlib import sha256
from tempfile import TemporaryFile
from typing import Any
from zipfile import ZipFile

import httpx

FORBIDDEN_EXPORT_FIELDS = frozenset(
    {
        "api_key",
        "client_instance_id_hash",
        "credential_hash",
        "idempotency_hash",
        "idempotency_key",
        "lease_expires_at",
        "lease_id",
        "lease_owner",
        "object_key",
        "password",
        "request_hash",
        "secret",
        "token",
    }
)
MAX_HTTP1_FIXED_LENGTH_RESPONSE_BYTES = 32 * 1024 * 1024


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _sha256_file(file) -> str:
    digest = sha256()
    file.seek(0)
    while chunk := file.read(1024 * 1024):
        digest.update(chunk)
    file.seek(0)
    return digest.hexdigest()


def _assert_safe_path(path: str) -> None:
    assert path and not path.startswith("/"), f"unsafe absolute archive path: {path}"
    assert ".." not in path.split("/"), f"unsafe parent archive path: {path}"


def _assert_archive(file, export: dict[str, Any], account_id: str) -> dict[str, Any]:
    assert _sha256_file(file) == export["archive_sha256"]
    file.seek(0)
    with ZipFile(file) as bundle:
        names = bundle.namelist()
        assert len(names) == len(set(names)), "archive contains duplicate paths"
        assert "manifest.json" in names
        assert "data/account.json" in names
        assert any(name.startswith("media/") for name in names), (
            "populated production account export contains no retained media"
        )
        for name in names:
            _assert_safe_path(name)

        manifest_bytes = bundle.read("manifest.json")
        assert sha256(manifest_bytes).hexdigest() == export["manifest_sha256"]
        manifest = json.loads(manifest_bytes)
        assert manifest["format_version"] == "foodlog-account-export-v1"
        assert manifest["export_id"] == export["id"]
        entries = manifest["entries"]
        assert entries == sorted(entries, key=lambda entry: entry["path"])
        assert set(names) == {"manifest.json", *(entry["path"] for entry in entries)}

        kinds: set[str] = set()
        for entry in entries:
            content = bundle.read(entry["path"])
            assert len(content) == entry["size"]
            assert sha256(content).hexdigest() == entry["sha256"]
            kinds.add(entry["kind"])
            if entry["kind"] == "json":
                payload = json.loads(content)
                forbidden = FORBIDDEN_EXPORT_FIELDS.intersection(_walk_keys(payload))
                assert not forbidden, f"exported operational fields: {sorted(forbidden)}"
                for key, value in _walk_account_ids(payload):
                    assert value == account_id, f"foreign account data at {key}: {value}"
    return {"entries": len(entries), "kinds": sorted(kinds)}


def _walk_account_ids(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "account_id":
                yield key, child
            yield from _walk_account_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_account_ids(child)


def _wait_for_export(
    client: httpx.Client,
    export_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    observed: list[str] = []
    while time.monotonic() < deadline:
        response = client.get(f"/v1/exports/{export_id}")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        account_export = response.json()
        status = account_export["status"]
        if not observed or observed[-1] != status:
            observed.append(status)
        if status == "completed":
            account_export["observed_statuses"] = observed
            return account_export
        assert status != "failed", account_export
        time.sleep(1)
    raise AssertionError(f"export did not complete within {timeout_seconds}s: {observed}")


def smoke(args: argparse.Namespace) -> None:
    firebase_response = httpx.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
        params={"key": args.firebase_api_key},
        headers={"Origin": args.origin, "Referer": f"{args.origin}/"},
        json={
            "email": os.environ["FOODLOG_SMOKE_EMAIL"],
            "password": os.environ["FOODLOG_SMOKE_PASSWORD"],
            "returnSecureToken": True,
        },
        timeout=30,
    )
    firebase_response.raise_for_status()
    token = firebase_response.json()["idToken"]

    unauthenticated = httpx.post(
        f"{args.api_url}/v1/exports",
        headers={"Idempotency-Key": args.idempotency_key},
        timeout=30,
    )
    assert unauthenticated.status_code == 401, unauthenticated.text

    with httpx.Client(
        base_url=args.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        timeout=120,
    ) as client:
        account_response = client.post("/v1/accounts")
        assert account_response.status_code == 200, account_response.text
        account_id = account_response.json()["id"]

        request_response = client.post(
            "/v1/exports",
            headers={"Idempotency-Key": args.idempotency_key},
        )
        assert request_response.status_code in {200, 202}, request_response.text
        assert request_response.headers["cache-control"] == "no-store"
        requested = request_response.json()
        assert requested["snapshot_at"] == requested["requested_at"]
        export_id = requested["id"]

        export = _wait_for_export(
            client,
            export_id,
            timeout_seconds=args.timeout_seconds,
        )
        assert export["archive_size"] > 0
        assert export["expires_at"] > export["completed_at"]

        download_path = f"/v1/exports/{export_id}/download"
        unauthenticated_download = httpx.get(f"{args.api_url}{download_path}", timeout=30)
        assert unauthenticated_download.status_code == 401

        with TemporaryFile("w+b") as archive:
            with client.stream("GET", download_path) as response:
                assert response.status_code == 200, (
                    f"full download returned {response.status_code}"
                )
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["content-type"] == "application/zip"
                if export["archive_size"] <= MAX_HTTP1_FIXED_LENGTH_RESPONSE_BYTES:
                    assert response.headers["content-length"] == str(
                        export["archive_size"]
                    )
                else:
                    assert "content-length" not in response.headers
                assert response.headers["accept-ranges"] == "bytes"
                assert "attachment" in response.headers["content-disposition"]
                assert response.headers["x-content-type-options"] == "nosniff"
                for chunk in response.iter_bytes():
                    archive.write(chunk)
            assert archive.tell() == export["archive_size"]
            archive_evidence = _assert_archive(archive, export, account_id)
            archive.seek(0)
            expected_prefix = archive.read(64)

        partial = client.get(download_path, headers={"Range": "bytes=0-63"})
        assert partial.status_code == 206, partial.text
        assert partial.content == expected_prefix
        assert partial.headers["content-range"] == f"bytes 0-63/{export['archive_size']}"
        assert partial.headers["content-length"] == "64"
        assert partial.headers["cache-control"] == "private, no-store"

        invalid = client.get(
            download_path,
            headers={"Range": f"bytes={export['archive_size']}-"},
        )
        assert invalid.status_code == 416, invalid.text
        assert invalid.content == b""
        assert invalid.headers["content-range"] == f"bytes */{export['archive_size']}"

        audit_response = client.get("/v1/audit-events?limit=200")
        assert audit_response.status_code == 200, audit_response.text
        matching_actions = {
            event["action"]
            for event in audit_response.json()
            if event["subject_id"] == export_id
        }
        assert "account_export.requested" in matching_actions
        assert "account_export.downloaded" in matching_actions

    print(f"account_id={account_id}")
    print(f"export_id={export_id}")
    print(f"request_status={request_response.status_code}")
    print(f"observed_statuses={','.join(export['observed_statuses'])}")
    print(f"archive_size={export['archive_size']}")
    print(f"archive_sha256={export['archive_sha256']}")
    print(f"manifest_sha256={export['manifest_sha256']}")
    print(f"manifest_entries={archive_evidence['entries']}")
    print(f"entry_kinds={','.join(archive_evidence['kinds'])}")
    print("full_download=verified")
    print("range_download=verified")
    print("invalid_range=416")
    print("tenant_scope=verified")
    print("operational_secrets=absent")
    print("export_audit=verified")
    print("unauthenticated_status=401")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
