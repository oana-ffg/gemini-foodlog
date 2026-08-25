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
        return VerifiedIdentity(
            uid="firebase-user-a",
            email_verified=True,
            email="user-a@example.test",
        )


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


class MappingTokenVerifier:
    def __init__(self, identities: dict[str, VerifiedIdentity]) -> None:
        self._identities = identities

    async def verify(self, token: str) -> VerifiedIdentity:
        try:
            return self._identities[token]
        except KeyError as error:
            raise InvalidAuthenticationToken from error


def firebase_test_client(token_verifier: Any, **settings_overrides: Any) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                environment="test",
                auth_backend="firebase",
                firebase_project_id="test-firebase-project",
                **settings_overrides,
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


def test_firebase_verifier_returns_verified_uid_and_normalized_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def verify_id_token(token: str, app: object) -> dict[str, str]:
        calls.append((token, app))
        return {
            "uid": "verified-user",
            "email": "  Mixed.Case@Example.Test  ",
            "email_verified": "not-a-boolean",
        }

    fake_app = object()
    monkeypatch.setattr(firebase_auth, "verify_id_token", verify_id_token)
    verifier = FirebaseIdentityTokenVerifier(
        "test-firebase-project",
        firebase_app=cast(App, fake_app),
    )

    assert asyncio.run(verifier.verify("signed-token")) == VerifiedIdentity(
        uid="verified-user",
        email_verified=False,
        email="mixed.case@example.test",
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


def test_launch_mail_consent_records_decline_grant_and_idempotent_retry() -> None:
    headers = {"Authorization": "Bearer valid-id-token"}
    with firebase_test_client(AcceptingTokenVerifier()) as client:
        account = client.post("/v1/accounts", headers=headers)
        declined = client.post(
            "/v1/consents/launch-mail",
            headers=headers,
            json={"granted": False},
        )
        declined_retry = client.post(
            "/v1/consents/launch-mail",
            headers=headers,
            json={"granted": False},
        )
        granted = client.post(
            "/v1/consents/launch-mail",
            headers=headers,
            json={"granted": True},
        )

    assert account.status_code == 200
    assert declined.status_code == declined_retry.status_code == granted.status_code == 200
    assert declined.json()["id"] == declined_retry.json()["id"]
    assert declined.json()["id"] != granted.json()["id"]
    assert declined.json()["granted"] is False
    assert granted.json()["granted"] is True
    assert granted.json()["kind"] == "launch_mail"
    assert granted.json()["policy_version"] == "launch-interest-v1"
    assert granted.json()["email_normalized"] == "user-a@example.test"


def test_launch_mail_consent_requires_account_and_verified_email_claim() -> None:
    no_email = MappingTokenVerifier(
        {
            "no-email-token": VerifiedIdentity(
                uid="firebase-user-without-email",
                email_verified=True,
            )
        }
    )
    with firebase_test_client(no_email) as client:
        missing_account = client.post(
            "/v1/consents/launch-mail",
            headers={"Authorization": "Bearer no-email-token"},
            json={"granted": True},
        )

    assert missing_account.status_code == 400
    assert missing_account.json() == {"detail": "verified_email_required"}

    with firebase_test_client(AcceptingTokenVerifier()) as client:
        not_provisioned = client.post(
            "/v1/consents/launch-mail",
            headers={"Authorization": "Bearer valid-id-token"},
            json={"granted": True},
        )

    assert not_provisioned.status_code == 404


def test_waitlist_requires_full_capacity_and_explicit_affirmative_join() -> None:
    verifier = MappingTokenVerifier(
        {
            "admitted-token": VerifiedIdentity(
                uid="admitted-user",
                email_verified=True,
                email="admitted@example.test",
            ),
            "waiting-token": VerifiedIdentity(
                uid="waiting-user",
                email_verified=True,
                email="waiting@example.test",
            ),
        }
    )
    admitted_headers = {"Authorization": "Bearer admitted-token"}
    waiting_headers = {"Authorization": "Bearer waiting-token"}
    with firebase_test_client(verifier, public_account_limit=1) as client:
        too_early = client.post(
            "/v1/waitlist",
            headers=waiting_headers,
            json={"join": True},
        )
        admitted = client.post("/v1/accounts", headers=admitted_headers)
        capacity_rejection = client.post("/v1/accounts", headers=waiting_headers)
        non_affirmative = client.post(
            "/v1/waitlist",
            headers=waiting_headers,
            json={"join": False},
        )
        joined = client.post(
            "/v1/waitlist",
            headers=waiting_headers,
            json={"join": True},
        )
        joined_retry = client.post(
            "/v1/waitlist",
            headers=waiting_headers,
            json={"join": True},
        )
        admitted_cannot_join = client.post(
            "/v1/waitlist",
            headers=admitted_headers,
            json={"join": True},
        )

    assert too_early.status_code == 409
    assert too_early.json() == {"detail": "signup_capacity_available"}
    assert admitted.status_code == 200
    assert capacity_rejection.status_code == 409
    assert capacity_rejection.json() == {"detail": "signup_capacity_exhausted"}
    assert non_affirmative.status_code == 422
    assert joined.status_code == joined_retry.status_code == 200
    assert joined.json()["id"] == joined_retry.json()["id"]
    assert joined.json()["mailing_list_opt_in"] is True
    assert joined.json()["reason"] == "capacity"
    assert joined.json()["policy_version"] == "capacity-waitlist-v1"
    assert admitted_cannot_join.status_code == 409
    assert admitted_cannot_join.json() == {"detail": "account_already_provisioned"}
