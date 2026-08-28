import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import firebase_admin
from firebase_admin import App
from firebase_admin import auth as firebase_auth
from firebase_admin.exceptions import FirebaseError
from google.auth.credentials import Credentials


class InvalidAuthenticationToken(Exception):
    """Raised when a presented Firebase identity token cannot be trusted."""


def normalize_verified_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if not normalized or len(normalized) > 320:
        return None
    return normalized


def firebase_app_for_project(
    project_id: str,
    *,
    credential: Credentials | None = None,
) -> App:
    app_name = f"foodlog-auth-{project_id}"
    try:
        return firebase_admin.get_app(app_name)
    except ValueError:
        return firebase_admin.initialize_app(
            credential=credential,
            options={"projectId": project_id},
            name=app_name,
        )


@dataclass(frozen=True)
class VerifiedIdentity:
    uid: str
    email_verified: bool
    email: str | None = None
    authenticated_at: datetime | None = None

    def was_recently_authenticated(
        self,
        *,
        now: datetime,
        maximum_age: timedelta,
    ) -> bool:
        authenticated_at = self.authenticated_at
        if authenticated_at is None or authenticated_at.tzinfo is None:
            return False
        normalized = authenticated_at.astimezone(UTC)
        return now - maximum_age <= normalized <= now + timedelta(minutes=1)


class IdentityTokenVerifier(Protocol):
    async def verify(self, token: str) -> VerifiedIdentity: ...


class FirebaseIdentityTokenVerifier:
    def __init__(self, project_id: str, *, firebase_app: App | None = None) -> None:
        self._app = firebase_app or firebase_app_for_project(project_id)

    async def verify(self, token: str) -> VerifiedIdentity:
        try:
            claims = await asyncio.to_thread(
                firebase_auth.verify_id_token,
                token,
                self._app,
            )
        except (ValueError, FirebaseError) as error:
            raise InvalidAuthenticationToken from error

        uid = claims.get("uid")
        if not isinstance(uid, str) or not uid or len(uid) > 128:
            raise InvalidAuthenticationToken
        return VerifiedIdentity(
            uid=uid,
            email_verified=claims.get("email_verified") is True,
            email=normalize_verified_email(claims.get("email")),
            authenticated_at=self._authentication_time(claims.get("auth_time")),
        )

    @staticmethod
    def _authentication_time(value: object) -> datetime | None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
