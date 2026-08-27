import json

import pytest

from foodlog_backend.secret_denial_smoke import main


def test_secret_denial_smoke_never_reads_or_logs_injected_values(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    sentinel = "must-never-enter-output"
    monkeypatch.setenv("FOODLOG_FORBIDDEN_PUSHOVER_APP_TOKEN", sentinel)
    monkeypatch.setenv("FOODLOG_FORBIDDEN_PUSHOVER_USER_KEY", sentinel)

    with pytest.raises(RuntimeError, match="forbidden runtime identity"):
        main()

    output = capfd.readouterr().out
    assert sentinel not in output
    assert json.loads(output) == {
        "event": "secret_access_boundary_broken",
        "outcome": "unexpected_container_start",
        "schema": "foodlog_operational_event_v1",
        "service": "secret_denial_smoke",
        "severity": "ERROR",
    }
