import pytest
from google.adk.events import Event
from google.genai import types

from foodlog_agent.event_reasoning import (
    InvalidModelOutputError,
    _validated_response,
    event_bundle,
)
from foodlog_agent.prompt import INSTRUCTION, PROMPT_VERSION
from foodlog_agent.smoke import (
    MAX_LLM_CALLS,
    _structured_response,
    _validate_source_identities,
    main,
    smoke_event_bundle,
)
from foodlog_backend.inference_schema import ActivityMealInferenceV1
from tests.inference_fixtures import base_payload


def test_smoke_bundle_records_prompt_and_exact_source_identities() -> None:
    bundle = smoke_event_bundle()
    assert bundle["prompt_version"] == PROMPT_VERSION
    assert bundle["event"]["event_id"] == "adk-smoke-event-v1"
    assert bundle["event"]["captures"][0]["capture_id"] == "adk-smoke-capture-v1"
    assert MAX_LLM_CALLS == 3


def test_prompt_explicitly_couples_questions_to_uncertain_confidence() -> None:
    assert PROMPT_VERSION == "food-event-v5"
    assert "question field MUST" in INSTRUCTION
    assert 'null when confidence is "likely" or "confident"' in INSTRUCTION
    assert 'confidence is exactly "uncertain"' in INSTRUCTION
    assert "harmless ambiguity" in INSTRUCTION
    assert "purchase or time-bounded user-note evidence" in INSTRUCTION
    assert "candidate_labels MUST start with the exact best_guess" in INSTRUCTION


def test_smoke_rejects_context_and_knowledge_ids_absent_from_input() -> None:
    payload = base_payload()
    inference = ActivityMealInferenceV1.model_validate(payload)
    bundle = {
        "context": {
            "recent_purchases": [{"purchase_id": "purchase-001"}],
            "household_knowledge": [],
        }
    }
    _validate_source_identities(inference, bundle)

    payload["assumptions"] = [
        {
            "id": "asm_fake",
            "description": "An unsupported household rule.",
            "knowledge_revision_id": "revision-invented",
        }
    ]
    invented = ActivityMealInferenceV1.model_validate(payload)
    try:
        _validate_source_identities(invented, bundle)
    except RuntimeError as error:
        assert "invented household knowledge revision ID" in str(error)
    else:
        raise AssertionError("an invented household knowledge revision passed smoke validation")


def test_final_adk_json_is_revalidated_by_the_product_schema() -> None:
    payload = base_payload()
    event = Event(
        invocation_id="invocation-001",
        author="food_event_reasoner",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=ActivityMealInferenceV1(**payload).model_dump_json())],
        ),
    )
    parsed = _structured_response(event)
    assert parsed.schema_version == "activity-meal-inference-v1"
    assert parsed.best_guess == "Air-fried steak"


def test_malformed_model_json_is_classified_as_repairable_invalid_output() -> None:
    malformed = Event(
        invocation_id="invocation-malformed",
        author="food_event_reasoner",
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text='{"schema_version":"wrong"}')],
        ),
    )
    bundle = event_bundle(event_id="event-001", capture_ids=["capture-001"])

    with pytest.raises(InvalidModelOutputError, match="ValidationError"):
        _validated_response(
            final_event=malformed,
            event_id="event-001",
            capture_ids=["capture-001"],
            bundle=bundle,
        )


def test_cli_refuses_an_accidental_billable_invocation() -> None:
    try:
        main([])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("the smoke command accepted an unconfirmed billable invocation")
