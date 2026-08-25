import asyncio
from typing import Protocol

import firebase_admin
from firebase_admin import App
from firebase_admin import auth as firebase_auth
from firebase_admin.exceptions import FirebaseError


class InvalidAuthenticationToken(Exception):
    """Raised when a presented Firebase identity token cannot be trusted."""


class IdentityTokenVerifier(Protocol):
    async def verify(self, token: str) -> str: ...


class FirebaseIdentityTokenVerifier:
    def __init__(self, project_id: str, *, firebase_app: App | None = None) -> None:
        self._app = firebase_app or self._get_or_initialize_app(project_id)

    @staticmethod
    def _get_or_initialize_app(project_id: str) -> App:
        app_name = f"foodlog-auth-{project_id}"
        try:
            return firebase_admin.get_app(app_name)
        except ValueError:
            return firebase_admin.initialize_app(
                options={"projectId": project_id},
                name=app_name,
            )

    async def verify(self, token: str) -> str:
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
        return uid
