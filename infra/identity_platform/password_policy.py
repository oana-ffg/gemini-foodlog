#!/usr/bin/env python3
"""Reconcile the one FoodLog Identity Platform password policy safely."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
ADMIN_API_ROOT = "https://identitytoolkit.googleapis.com/admin/v2"
MIN_PASSWORD_LENGTH = 12
DESIRED_POLICY: dict[str, Any] = {
    "passwordPolicyEnforcementState": "ENFORCE",
    "forceUpgradeOnSignin": False,
    "passwordPolicyVersions": [
        {"customStrengthOptions": {"minPasswordLength": MIN_PASSWORD_LENGTH}}
    ],
}
STRENGTH_OPTION_FIELDS = frozenset({
    "minPasswordLength",
    "maxPasswordLength",
    "containsLowercaseCharacter",
    "containsUppercaseCharacter",
    "containsNumericCharacter",
    "containsNonAlphanumericCharacter",
})


class PasswordPolicyError(RuntimeError):
    """Raised when policy reconciliation cannot be proved safe and complete."""


@dataclass(frozen=True)
class PolicyStatus:
    enforcement_state: str | None
    force_upgrade_on_signin: bool | None
    min_length: int | None
    max_length: int | None
    contains_lowercase: bool | None
    contains_uppercase: bool | None
    contains_numeric: bool | None
    contains_non_alphanumeric: bool | None
    unexpected_strength_options: frozenset[str]
    malformed_strength_options: bool

    @property
    def matches_desired(self) -> bool:
        return (
            self.enforcement_state == "ENFORCE"
            and self.force_upgrade_on_signin is False
            and self.min_length == MIN_PASSWORD_LENGTH
            and self.max_length is None
            and self.contains_lowercase is False
            and self.contains_uppercase is False
            and self.contains_numeric is False
            and self.contains_non_alphanumeric is False
            and not self.unexpected_strength_options
            and not self.malformed_strength_options
        )


def parse_policy(response: dict[str, Any]) -> PolicyStatus:
    policy = response.get("passwordPolicyConfig")
    if not isinstance(policy, dict):
        return PolicyStatus(
            None, None, None, None, None, None, None, None, frozenset(), False
        )
    versions = policy.get("passwordPolicyVersions")
    version = versions[0] if isinstance(versions, list) and len(versions) == 1 else None
    options = version.get("customStrengthOptions") if isinstance(version, dict) else None
    if not isinstance(options, dict):
        options = {}
    force_upgrade = policy.get("forceUpgradeOnSignin", False)
    minimum = options.get("minPasswordLength")
    maximum = options.get("maxPasswordLength")

    def optional_bool(name: str) -> bool | None:
        value = options.get(name, False)
        return value if isinstance(value, bool) else None

    return PolicyStatus(
        enforcement_state=(
            policy.get("passwordPolicyEnforcementState")
            if isinstance(policy.get("passwordPolicyEnforcementState"), str)
            else None
        ),
        force_upgrade_on_signin=(force_upgrade if isinstance(force_upgrade, bool) else None),
        min_length=(
            minimum
            if isinstance(minimum, int) and not isinstance(minimum, bool)
            else None
        ),
        max_length=(
            maximum
            if isinstance(maximum, int) and not isinstance(maximum, bool)
            else None
        ),
        contains_lowercase=optional_bool("containsLowercaseCharacter"),
        contains_uppercase=optional_bool("containsUppercaseCharacter"),
        contains_numeric=optional_bool("containsNumericCharacter"),
        contains_non_alphanumeric=optional_bool("containsNonAlphanumericCharacter"),
        unexpected_strength_options=frozenset(options) - STRENGTH_OPTION_FIELDS,
        malformed_strength_options=(
            "maxPasswordLength" in options
            and not (
                isinstance(maximum, int)
                and not isinstance(maximum, bool)
            )
        ),
    )


def access_token(project_id: str) -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token", f"--project={project_id}"],
        check=True,
        capture_output=True,
        text=True,
    )
    token = result.stdout.strip()
    if not token:
        raise PasswordPolicyError("gcloud returned an empty access token")
    return token


def request_json(
    *,
    project_id: str,
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query = {"fields": "passwordPolicyConfig"}
    if method == "PATCH":
        query["updateMask"] = "passwordPolicyConfig"
    url = (
        f"{ADMIN_API_ROOT}/projects/{quote(project_id, safe='')}/config?"
        f"{urlencode(query)}"
    )
    body = json.dumps(payload, separators=(",", ":")).encode() if payload else None
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": project_id,
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            decoded = json.load(response)
    except HTTPError as error:
        raise PasswordPolicyError(
            f"Identity Platform returned HTTP {error.code} during {method}"
        ) from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise PasswordPolicyError(
            f"Identity Platform {method} did not return a verified JSON response"
        ) from error
    if not isinstance(decoded, dict):
        raise PasswordPolicyError("Identity Platform returned an invalid policy response")
    return decoded


def reconcile(project_id: str, *, apply: bool) -> PolicyStatus:
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise PasswordPolicyError("project ID has an invalid format")
    token = access_token(project_id)
    current = parse_policy(
        request_json(project_id=project_id, token=token, method="GET")
    )
    if current.matches_desired:
        return current
    if not apply:
        raise PasswordPolicyError("Identity Platform password policy differs from source")
    request_json(
        project_id=project_id,
        token=token,
        method="PATCH",
        payload={"passwordPolicyConfig": DESIRED_POLICY},
    )
    verified = parse_policy(
        request_json(project_id=project_id, token=token, method="GET")
    )
    if not verified.matches_desired:
        raise PasswordPolicyError("Identity Platform policy did not match after reconciliation")
    return verified


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        status = reconcile(args.project, apply=args.apply)
    except (PasswordPolicyError, subprocess.CalledProcessError) as error:
        print(f"password policy verification failed: {error}", file=sys.stderr)
        return 1
    action = "reconciled" if args.apply else "verified"
    print(
        f"password policy {action}: minimum {status.min_length}, "
        "new passwords enforced, existing passwords not force-upgraded"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
