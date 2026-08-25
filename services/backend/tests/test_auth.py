import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from firebase_admin import App
from firebase_admin import auth as firebase_auth
from pydantic import ValidationError

from foodlog_backend.app import create_app
from foodlog_backend.auth import (
    FirebaseIdentityTokenVerifier,
    InvalidAuthenticationToken,
    VerifiedIdentity,
)
from foodlog_backend.settings import Settings


class AcceptingTokenVerifier:
    async def verify(self, token: str) -> VerifiedIdentity:
        assert token == "valid-id-token"
        return VerifiedIdentity(uid="firebase-user-a", email_verified=True)


class UnverifiedTokenVerifier:
    async def verify(self, token: str) -> VerifiedIdentity:
        assert token == "unverified-id-token"
        return VerifiedIdentity(uid="firebase-user-a", email_verified=False)


class RejectingTokenVerifier:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def verify(self, token: str) -> VerifiedIdentity:
        self.tokens.append(token)
        raise InvalidAuthenticationToken


def firebase_test_client(token_verifier: Any) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                environment="test",
                auth_backend="firebase",
                firebase_project_id="test-firebase-project",
            ),
            token_verifier=token_verifier,
        )
    )


def test_verified_bearer_token_is_the_only_source_of_user_identity() -> None:
    with firebase_test_client(AcceptingTokenVerifier()) as client:
        response = client.post(
            "/v1/accounts",
            headers={
                "Authorization": "Bearer valid-id-token",
                "X-FoodLog-Local-User": "attacker-selected-user",
            },
        )

    assert response.status_code == 200
    assert response.json()["owner_user_id"] == "firebase-user-a"


def test_unverified_email_cannot_reach_any_private_route() -> None:
    headers = {"Authorization": "Bearer unverified-id-token"}
    with firebase_test_client(UnverifiedTokenVerifier()) as client:
        account = client.post("/v1/accounts", headers=headers)
        journal = client.get("/v1/journal", headers=headers)

    assert account.status_code == journal.status_code == 403
    assert account.json() == journal.json() == {"detail": "email_verification_required"}


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Basic credentials",
        "Bearer",
        "Bearer token with spaces",
        f"Bearer {'x' * 8193}",
    ],
)
def test_missing_or_malformed_bearer_token_fails_closed(
    authorization: str | None,
) -> None:
    headers = {"Authorization": authorization} if authorization is not None else {}
    with firebase_test_client(AcceptingTokenVerifier()) as client:
        response = client.post("/v1/accounts", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("rejected_token", ["forged", "expired", "wrong-project"])
def test_untrusted_tokens_return_the_same_unauthorized_response(rejected_token: str) -> None:
    verifier = RejectingTokenVerifier()
    with firebase_test_client(verifier) as client:
        response = client.post(
            "/v1/accounts",
            headers={"Authorization": f"Bearer {rejected_token}"},
        )

    assert verifier.tokens == [rejected_token]
    assert response.status_code == 401
    assert response.json() == {"detail": "A valid bearer token is required"}


def test_firebase_cors_allows_bearer_tokens_but_not_local_identity_headers() -> None:
    origin = "http://127.0.0.1:5173"
    with firebase_test_client(AcceptingTokenVerifier()) as client:
        bearer_preflight = client.options(
            "/v1/accounts",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        local_header_preflight = client.options(
            "/v1/accounts",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-foodlog-local-user",
            },
        )

    assert bearer_preflight.status_code == 200
    assert "Authorization" in bearer_preflight.headers["access-control-allow-headers"]
    assert local_header_preflight.status_code == 400


def test_firebase_verifier_returns_only_the_verified_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def verify_id_token(token: str, app: object) -> dict[str, str]:
        calls.append((token, app))
        return {"uid": "verified-user", "email": "not-used@example.test"}

    fake_app = object()
    monkeypatch.setattr(firebase_auth, "verify_id_token", verify_id_token)
    verifier = FirebaseIdentityTokenVerifier(
        "test-firebase-project",
        firebase_app=cast(App, fake_app),
    )

    assert asyncio.run(verifier.verify("signed-token")) == VerifiedIdentity(
        uid="verified-user",
        email_verified=False,
    )
    assert calls == [("signed-token", fake_app)]


@pytest.mark.parametrize(
    "firebase_error",
    [
        firebase_auth.InvalidIdTokenError("forged or wrong-project token"),
        firebase_auth.ExpiredIdTokenError("expired token", None),
        firebase_auth.CertificateFetchError("certificate fetch failed", None),
    ],
)
def test_firebase_sdk_rejections_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    firebase_error: Exception,
) -> None:
    def reject_token(_token: str, _app: object) -> dict[str, object]:
        raise firebase_error

    monkeypatch.setattr(firebase_auth, "verify_id_token", reject_token)
    verifier = FirebaseIdentityTokenVerifier(
        "test-firebase-project",
        firebase_app=cast(App, object()),
    )

    with pytest.raises(InvalidAuthenticationToken):
        asyncio.run(verifier.verify("untrusted-token"))


@pytest.mark.parametrize(
    "claims",
    [{}, {"uid": ""}, {"uid": 123}, {"uid": "x" * 129}],
)
def test_firebase_verifier_rejects_invalid_uid_claims(
    monkeypatch: pytest.MonkeyPatch,
    claims: dict[str, object],
) -> None:
    verifier_call = cast(
        Callable[[str, object], dict[str, object]],
        lambda _token, _app: claims,
    )
    monkeypatch.setattr(firebase_auth, "verify_id_token", verifier_call)
    verifier = FirebaseIdentityTokenVerifier(
        "test-firebase-project",
        firebase_app=cast(App, object()),
    )

    with pytest.raises(InvalidAuthenticationToken):
        asyncio.run(verifier.verify("signed-token"))


def test_production_rejects_local_authentication() -> None:
    with pytest.raises(ValidationError, match="Production requires Firebase"):
        Settings(
            environment="production",
            auth_backend="local",
            storage_backend="gcp",
            gcp_project_id="gemini-foodlog-2026",
            media_bucket="gemini-foodlog-2026-media-163029863855",
        )


def test_local_authentication_rejects_unused_token_verifier() -> None:
    with pytest.raises(ValueError, match="cannot be configured"):
        create_app(
            Settings(environment="test"),
            token_verifier=AcceptingTokenVerifier(),
        )
