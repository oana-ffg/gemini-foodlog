import pytest

from foodlog_backend.model_probe import estimate_cost_usd, required_project, token_count


def test_required_project_rejects_missing_or_blank_values() -> None:
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        required_project(None)
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        required_project("  ")


def test_required_project_normalizes_whitespace() -> None:
    assert required_project(" gemini-foodlog-2026 ") == "gemini-foodlog-2026"


def test_token_count_normalizes_absent_usage_to_zero() -> None:
    assert token_count(None) == 0
    assert token_count(7) == 7


def test_estimate_cost_includes_response_and_thinking_tokens() -> None:
    assert estimate_cost_usd(
        prompt_tokens=23,
        response_tokens=9,
        thinking_tokens=4,
    ) == pytest.approx(0.0000726)
