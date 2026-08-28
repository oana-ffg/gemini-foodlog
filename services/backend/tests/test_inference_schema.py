from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from foodlog_backend.inference_schema import (
    ActivityMealInferenceModelOutputV1,
    ActivityMealInferenceV1,
)
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
    inference = ActivityMealInferenceModelOutputV1.model_validate(payload)
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


def test_unknown_activity_rejects_a_question_even_when_it_names_hypotheses() -> None:
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


def test_model_facing_schema_reduces_complexity_without_weakening_validation() -> None:
    strict_schema = json.dumps(ActivityMealInferenceV1.model_json_schema())
    model_schema = json.dumps(ActivityMealInferenceModelOutputV1.model_json_schema())
    for keyword in ("maxLength", "maxItems", "minimum", "pattern", "title"):
        assert keyword in strict_schema
        assert keyword not in model_schema
    assert "allowed_actions" in json.loads(strict_schema)["properties"]
    assert "allowed_actions" not in json.loads(model_schema)["properties"]

    invalid_payload = base_payload()
    invalid_payload["source_capture_ids"] = []
    with pytest.raises(ValidationError):
        ActivityMealInferenceModelOutputV1.model_validate(invalid_payload)


@pytest.mark.parametrize(
    ("kind", "best_guess", "components", "expected_actions"),
    [
        (
            "tentative_meal",
            "Air-fried steak",
            None,
            ["confirm_guess", "correct", "discard_not_cooking"],
        ),
        (
            "unknown_activity",
            None,
            [],
            ["correct", "discard_not_cooking"],
        ),
        (
            "likely_non_cooking",
            "Cat jumped onto the counter",
            [],
            ["correct", "discard_not_cooking"],
        ),
    ],
)
def test_model_output_derives_ui_actions_from_inference_state(
    kind: str,
    best_guess: str | None,
    components: list[object] | None,
    expected_actions: list[str],
) -> None:
    payload = base_payload()
    payload.update(kind=kind, best_guess=best_guess)
    if components is not None:
        payload.update(
            confidence="uncertain" if kind == "unknown_activity" else "likely",
            components=components,
            contextual_evidence=[],
            deductions=[],
            alternatives=[],
            question=None,
        )
    payload.pop("allowed_actions")

    inference = ActivityMealInferenceModelOutputV1.model_validate(payload)

    assert inference.allowed_actions == expected_actions


def test_planned_duck_context_can_justify_a_material_chicken_versus_duck_question() -> None:
    payload = deepcopy(base_payload())
    payload["best_guess"] = "Air-fried chicken"
    payload["components"][0].update(
        name="Chicken",
        ingredients=["pale poultry"],
        evidence_ids=["obs_meat", "ctx_duck", "ded_meat"],
    )
    payload["contextual_evidence"] = [
        {
            "id": "ctx_duck",
            "description": "An active user note says duck may be cooked today.",
            "source_kind": "user_note",
            "source_id": "note-planned-duck-001",
        }
    ]
    payload["deductions"][0]["evidence_ids"] = ["obs_meat", "ctx_duck"]
    payload["alternatives"] = [
        {
            "label": "Air-fried duck",
            "reason": "The pale meat and active planned-duck note make duck plausible.",
            "evidence_ids": ["obs_meat", "ctx_duck"],
        }
    ]
    payload["question"].update(
        prompt="Was this the usual chicken or the duck planned for today?",
        justification="The distinction changes the food identity recorded in the journal.",
        evidence_ids=["obs_meat", "ctx_duck"],
        candidate_labels=["Air-fried chicken", "Air-fried duck"],
        impact="changes_food_trigger_relevance",
    )

    inference = ActivityMealInferenceV1.model_validate(payload)

    assert inference.question is not None
    assert inference.question.candidate_labels == ["Air-fried chicken", "Air-fried duck"]
    assert inference.question.impact == "changes_food_trigger_relevance"


def test_harmless_uncertainty_can_remain_visible_without_interrupting_the_user() -> None:
    payload = deepcopy(base_payload())
    payload["question"] = None
    payload["alternatives"] = [
        {
            "label": "Pan-seared steak",
            "reason": "The exact appliance is outside the visible frame.",
            "evidence_ids": ["obs_meat"],
        }
    ]

    inference = ActivityMealInferenceModelOutputV1.model_validate(payload)

    assert inference.confidence == "uncertain"
    assert inference.question is None
    assert inference.alternatives[0].label == "Pan-seared steak"


def test_availability_context_cannot_resolve_a_visual_identity_tie() -> None:
    payload = deepcopy(base_payload())
    payload["confidence"] = "likely"
    payload["components"][0]["confidence"] = "likely"
    payload["question"] = None

    with pytest.raises(ValidationError, match="context cannot resolve"):
        ActivityMealInferenceModelOutputV1.model_validate(payload)


def test_historical_context_resolved_identity_tie_remains_readable() -> None:
    payload = deepcopy(base_payload())
    payload["confidence"] = "likely"
    payload["components"][0]["confidence"] = "likely"
    payload["question"] = None

    historical = ActivityMealInferenceV1.model_validate(payload)

    assert historical.confidence == "likely"
    assert historical.alternatives[0].label == "Air-fried lamb"


def test_context_resolved_visual_tie_requires_a_focused_question() -> None:
    payload = deepcopy(base_payload())
    payload["question"] = None

    with pytest.raises(ValidationError, match="requires a focused candidate question"):
        ActivityMealInferenceModelOutputV1.model_validate(payload)


def test_context_resolved_component_tie_must_remain_uncertain() -> None:
    payload = deepcopy(base_payload())
    payload["components"][0]["confidence"] = "likely"

    with pytest.raises(ValidationError, match="component alternative"):
        ActivityMealInferenceModelOutputV1.model_validate(payload)


def test_likely_visual_result_can_keep_non_decisive_context() -> None:
    payload = deepcopy(base_payload())
    payload["confidence"] = "likely"
    payload["components"][0]["confidence"] = "likely"
    payload["deductions"][0]["evidence_ids"] = ["obs_meat"]
    payload["components"][0]["evidence_ids"] = ["obs_meat", "ded_meat"]
    payload["question"] = None

    inference = ActivityMealInferenceModelOutputV1.model_validate(payload)

    assert inference.confidence == "likely"
    assert inference.contextual_evidence[0].source_id == "purchase-001"


@pytest.mark.parametrize(
    ("candidate_labels", "message"),
    [
        (["Air-fried lamb", "Air-fried steak"], "lead with the current best guess"),
        (["Air-fried steak", "Roast duck"], "exact named alternatives"),
        (["Air-fried steak", "Air-fried steak"], "must be unique"),
    ],
)
def test_focused_question_candidates_must_exactly_bind_concrete_hypotheses(
    candidate_labels: list[str],
    message: str,
) -> None:
    payload = deepcopy(base_payload())
    payload["question"]["candidate_labels"] = candidate_labels

    with pytest.raises(ValidationError, match=message):
        ActivityMealInferenceV1.model_validate(payload)


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
            lambda value: value.update(alternatives=[]),
            "at least one named alternative",
        ),
        (
            lambda value: value["question"].update(evidence_ids=["ctx_order"]),
            "include every cited alternative's evidence",
        ),
    ],
)
def test_malformed_outputs_fail_closed(mutate, message: str) -> None:
    payload = deepcopy(base_payload())
    mutate(payload)
    with pytest.raises(ValidationError, match=message):
        ActivityMealInferenceV1.model_validate(payload)
