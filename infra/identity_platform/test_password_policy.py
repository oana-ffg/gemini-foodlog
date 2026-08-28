import io
import json
import unittest
from unittest.mock import MagicMock, patch

from infra.identity_platform import password_policy


class Response:
    def __init__(self, value: dict) -> None:
        self._content = io.BytesIO(json.dumps(value).encode())

    def __enter__(self) -> io.BytesIO:
        return self._content

    def __exit__(self, *_args: object) -> None:
        return None


class PasswordPolicyTest(unittest.TestCase):
    def test_desired_policy_protects_new_users_without_locking_existing_users(self) -> None:
        self.assertEqual(password_policy.DESIRED_POLICY, {
            "passwordPolicyEnforcementState": "ENFORCE",
            "forceUpgradeOnSignin": False,
            "passwordPolicyVersions": [{
                "customStrengthOptions": {"minPasswordLength": 12},
            }],
        })

    def test_parse_policy_rejects_missing_and_boolean_lengths(self) -> None:
        self.assertFalse(password_policy.parse_policy({}).matches_desired)
        self.assertFalse(password_policy.parse_policy({
            "passwordPolicyConfig": {
                "passwordPolicyEnforcementState": "ENFORCE",
                "forceUpgradeOnSignin": False,
                "passwordPolicyVersions": [{
                    "customStrengthOptions": {"minPasswordLength": True},
                }],
            }
        }).matches_desired)

    def test_parse_policy_rejects_any_extra_strength_constraint(self) -> None:
        for extra in (
            {"maxPasswordLength": 12},
            {"containsLowercaseCharacter": True},
            {"containsUppercaseCharacter": True},
            {"containsNumericCharacter": True},
            {"containsNonAlphanumericCharacter": True},
            {"maxPasswordLength": "twelve"},
            {"futureStrengthOption": True},
        ):
            with self.subTest(extra=extra):
                options = {"minPasswordLength": 12, **extra}
                status = password_policy.parse_policy({
                    "passwordPolicyConfig": {
                        "passwordPolicyEnforcementState": "ENFORCE",
                        "forceUpgradeOnSignin": False,
                        "passwordPolicyVersions": [{
                            "customStrengthOptions": options,
                            "schemaVersion": 1,
                        }],
                    },
                })
                self.assertFalse(status.matches_desired)

    def test_parse_policy_treats_omitted_false_fields_as_api_defaults(self) -> None:
        status = password_policy.parse_policy({
            "passwordPolicyConfig": {
                "passwordPolicyEnforcementState": "ENFORCE",
                "passwordPolicyVersions": [{
                    "customStrengthOptions": {"minPasswordLength": 12},
                    "schemaVersion": 1,
                }],
            },
        })

        self.assertTrue(status.matches_desired)

    @patch.object(password_policy, "urlopen")
    def test_request_uses_bounded_fields_and_never_requests_hash_config(
        self,
        urlopen_mock: MagicMock,
    ) -> None:
        urlopen_mock.return_value = Response({"passwordPolicyConfig": {}})

        password_policy.request_json(
            project_id="gemini-foodlog-2026",
            token="not-logged",
            method="GET",
        )

        request = urlopen_mock.call_args.args[0]
        self.assertIn("fields=passwordPolicyConfig", request.full_url)
        self.assertNotIn("hash", request.full_url.lower())

    @patch.object(password_policy, "request_json")
    @patch.object(password_policy, "access_token", return_value="not-logged")
    def test_reconcile_applies_once_then_verifies(
        self,
        _token_mock: MagicMock,
        request_mock: MagicMock,
    ) -> None:
        request_mock.side_effect = [
            {},
            {"passwordPolicyConfig": password_policy.DESIRED_POLICY},
            {"passwordPolicyConfig": password_policy.DESIRED_POLICY},
        ]

        status = password_policy.reconcile("gemini-foodlog-2026", apply=True)

        self.assertTrue(status.matches_desired)
        self.assertEqual(
            [call.kwargs["method"] for call in request_mock.call_args_list],
            ["GET", "PATCH", "GET"],
        )

    @patch.object(password_policy, "request_json", return_value={})
    @patch.object(password_policy, "access_token", return_value="not-logged")
    def test_check_fails_closed_on_drift(
        self,
        _token_mock: MagicMock,
        _request_mock: MagicMock,
    ) -> None:
        with self.assertRaisesRegex(
            password_policy.PasswordPolicyError,
            "differs from source",
        ):
            password_policy.reconcile("gemini-foodlog-2026", apply=False)


if __name__ == "__main__":
    unittest.main()
