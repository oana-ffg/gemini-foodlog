from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from foodlog_agent.inference_schema import ActivityMealInferenceV1
from tests.inference_fixtures import base_payload


def test_tentative_meal_preserves_separate_reasoning_layers_and_focused_question() -> None:
    inference = ActivityMealInferenceV1.model_validate(base_payload())
    assert inference.best_guess == "Air-fried steak"
    assert inference.direct_observations[0].image_evidence[0].capture_id == "capture-001"
    assert inference.contextual_evidence[0].source_id == "purchase-001"
    assert inference.deductions[0].evidence_ids == ["obs_meat", "ctx_order"]
    assert inference.question is not None


def test_genuinely_unknown_activity_has_no_guess_question_or_confirmation() -> None:
    payload = base_payload()
    payload.update(
        kind="unknown_activity",
        best_guess=None,
        confidence="uncertain",
        components=[],
        contextual_evidence=[],
        deductions=[],
        alternatives=[],
        question=None,
        allowed_actions=["correct", "discard_not_cooking"],
    )
    inference = ActivityMealInferenceV1.model_validate(payload)
    assert inference.best_guess is None
    assert "confirm_guess" not in inference.allowed_actions


def test_likely_non_cooking_activity_can_be_discarded_but_not_confirmed_as_a_meal() -> None:
    payload = base_payload()
    payload.update(
        kind="likely_non_cooking",
        best_guess="Cat jumped onto the counter",
        confidence="likely",
        components=[],
        contextual_evidence=[],
        deductions=[],
        alternatives=[],
        question=None,
        allowed_actions=["correct", "discard_not_cooking"],
    )
    inference = ActivityMealInferenceV1.model_validate(payload)
    assert inference.best_guess == "Cat jumped onto the counter"
    assert "confirm_guess" not in inference.allowed_actions


@pytest.mark.parametrize("kind", ["unknown_activity", "likely_non_cooking"])
def test_non_confirmable_states_reject_the_meal_confirmation_action(kind: str) -> None:
    payload = base_payload()
    payload.update(
        kind=kind,
        best_guess=None if kind == "unknown_activity" else "Cat jumped onto the counter",
        confidence="uncertain" if kind == "unknown_activity" else "likely",
        components=[],
        contextual_evidence=[],
        deductions=[],
        alternatives=[],
        question=None,
        allowed_actions=["confirm_guess", "correct", "discard_not_cooking"],
    )
    with pytest.raises(ValidationError, match="cannot expose"):
        ActivityMealInferenceV1.model_validate(payload)


def test_unknown_activity_rejects_a_question_even_when_it_names_options() -> None:
    payload = base_payload()
    payload.update(
        kind="unknown_activity",
        best_guess=None,
        components=[],
        contextual_evidence=[],
        deductions=[],
        alternatives=[],
        allowed_actions=["correct", "discard_not_cooking"],
    )
    payload["question"]["evidence_ids"] = ["obs_meat"]
    with pytest.raises(ValidationError, match="only a tentative meal"):
        ActivityMealInferenceV1.model_validate(payload)


def test_zero_sized_image_region_is_rejected_without_vertex_exclusive_minimum() -> None:
    payload = base_payload()
    payload["direct_observations"][0]["image_evidence"][0]["region"]["width"] = 0
    with pytest.raises(ValidationError, match="image region dimensions must be positive"):
        ActivityMealInferenceV1.model_validate(payload)


def test_model_facing_schema_avoids_unsupported_exclusive_minimum() -> None:
    assert "exclusiveMinimum" not in json.dumps(ActivityMealInferenceV1.model_json_schema())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value.update(best_guess=None),
            "tentative meals require a best guess",
        ),
        (
            lambda value: value.update(allowed_actions=["correct", "discard_not_cooking"]),
            "tentative meals must expose confirm",
        ),
        (
            lambda value: value["direct_observations"][0]["image_evidence"][0].update(
                capture_id="capture-foreign"
            ),
            "declared source capture",
        ),
        (
            lambda value: value["deductions"][0].update(evidence_ids=["missing_evidence"]),
            "unknown evidence references",
        ),
        (
            lambda value: value["question"].update(prompt="What meal were you cooking?"),
            "distinguish specific hypotheses",
        ),
        (
            lambda value: value["question"].update(options=["Air-fried steak", "Duck"]),
            "existing hypotheses",
        ),
    ],
)
def test_malformed_outputs_fail_closed(mutate, message: str) -> None:
    payload = deepcopy(base_payload())
    mutate(payload)
    with pytest.raises(ValidationError, match=message):
        ActivityMealInferenceV1.model_validate(payload)
