import pytest
from google.adk.events import Event
from google.genai import types

from foodlog_agent.event_reasoning import (
    InvalidModelOutputError,
    _tool_context_source_ids,
    _validated_response,
    application_visible_model_request,
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
from foodlog_backend.inference_schema import ActivityMealInferenceV1, ContextSourceKind
from tests.inference_fixtures import base_payload


def test_smoke_bundle_records_prompt_and_exact_source_identities() -> None:
    bundle = smoke_event_bundle()
    assert bundle["prompt_version"] == PROMPT_VERSION
    assert bundle["event"]["event_id"] == "adk-smoke-event-v1"
    assert bundle["event"]["captures"][0]["capture_id"] == "adk-smoke-capture-v1"
    assert MAX_LLM_CALLS == 4


def test_trace_request_includes_the_application_owned_prompt_tools_and_schema() -> None:
    bundle = smoke_event_bundle()
    request = application_visible_model_request(
        bundle=bundle,
        purpose="deployment_smoke",
        event_revision=7,
    )

    assert request["model"] == "gemini-3.6-flash"
    assert request["system_instruction"] == INSTRUCTION
    assert request["user_content"] == bundle
    assert request["tools"] == [
        "get_current_event_evidence",
        "get_recent_meals",
        "get_recent_purchases",
        "get_active_user_context",
        "get_unresolved_reviews",
        "list_household_knowledge",
        "read_household_knowledge_page",
        "load_artifacts",
    ]
    assert "title" not in request["response_schema"]
    assert request["response_schema"]["properties"]["schema_version"]["const"] == (
        "activity-meal-inference-v1"
    )
    assert request["run_config"] == {
        "max_llm_calls": 4,
        "custom_metadata": {
            "prompt_version": PROMPT_VERSION,
            "purpose": "deployment_smoke",
            "event_revision": 7,
        },
    }


def test_prompt_explicitly_couples_questions_to_uncertain_confidence() -> None:
    assert PROMPT_VERSION == "food-event-v11"
    assert "Follow this exact bounded tool-turn plan" in INSTRUCTION
    assert "Never call more than four tools in one turn" in INSTRUCTION
    assert "select at most two relevant pages" in INSTRUCTION
    first_turn = INSTRUCTION.split("On the second tool", 1)[0]
    assert first_turn.count("get_") == 4
    assert "call get_current_event_evidence" in INSTRUCTION
    assert "get_recent_meals" in INSTRUCTION
    assert "get_recent_purchases" in INSTRUCTION
    assert "get_active_user_context" in INSTRUCTION
    assert "get_unresolved_reviews" in INSTRUCTION
    assert "list_household_knowledge" in INSTRUCTION
    assert "read_household_knowledge_page" in INSTRUCTION
    assert "summary is a selection aid, not evidence" in INSTRUCTION
    assert "question field MUST" in INSTRUCTION
    assert 'null when confidence is "likely" or "confident"' in INSTRUCTION
    assert 'confidence is exactly "uncertain"' in INSTRUCTION
    assert "harmless ambiguity" in INSTRUCTION
    assert "purchase or time-bounded user-note evidence" in INSTRUCTION
    assert "candidate_labels MUST start with the exact best_guess" in INSTRUCTION


def test_prompt_calibrates_specificity_for_degraded_single_frames() -> None:
    assert "weakest material visual distinction" in INSTRUCTION
    assert "not positive evidence for a specific" in INSTRUCTION
    assert "visible distinguishing feature" in INSTRUCTION
    assert 'return the concrete best guess as "uncertain"' in INSTRUCTION
    assert "without inferred food labels that the pixels do not establish" in INSTRUCTION
    assert "itself may support both candidates" in INSTRUCTION
    assert "materially unresolved distinction" in INSTRUCTION
    assert "cannot by itself identify what is in the current image" in INSTRUCTION
    assert 'keep confidence "uncertain"' in INSTRUCTION
    assert "even when one candidate is known to be available" in INSTRUCTION


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


def test_validation_accepts_only_exact_context_ids_returned_by_agent_tools() -> None:
    tool_event = Event(
        invocation_id="invocation-context-tools",
        author="food_event_reasoner",
        content=types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name="get_recent_meals",
                    response={"meals": [{"event_id": "recent-event-001"}]},
                ),
                types.Part.from_function_response(
                    name="get_recent_purchases",
                    response={"purchases": [{"purchase_id": "purchase-001"}]},
                ),
                types.Part.from_function_response(
                    name="get_active_user_context",
                    response={"notes": [{"note_id": "note-001"}]},
                ),
            ],
        ),
    )
    source_ids = _tool_context_source_ids(tool_event)
    payload = base_payload()
    payload["contextual_evidence"] = [
        {
            "id": "ctx_order",
            "description": "A recent meal provides a household comparison.",
            "source_kind": "recent_meal",
            "source_id": "recent-event-001",
        },
        {
            "id": "ctx_purchase",
            "description": "A final receipt contains a plausible ingredient.",
            "source_kind": "purchase",
            "source_id": "purchase-001",
        },
        {
            "id": "ctx_note",
            "description": "A time-bounded note says duck may be cooked tonight.",
            "source_kind": "user_note",
            "source_id": "note-001",
        },
    ]
    inference = ActivityMealInferenceV1.model_validate(payload)
    _validate_source_identities(
        inference, event_bundle(event_id="event-001", capture_ids=["capture-001"]), source_ids
    )

    payload["contextual_evidence"][1]["source_id"] = "invented-purchase"
    invented = ActivityMealInferenceV1.model_validate(payload)
    with pytest.raises(RuntimeError, match="invented purchase source ID"):
        _validate_source_identities(
            invented, event_bundle(event_id="event-001", capture_ids=["capture-001"]), source_ids
        )

    payload["contextual_evidence"][1]["source_id"] = "purchase-001"
    payload["contextual_evidence"][2]["source_id"] = "invented-note"
    invented = ActivityMealInferenceV1.model_validate(payload)
    with pytest.raises(RuntimeError, match="invented user_note source ID"):
        _validate_source_identities(
            invented, event_bundle(event_id="event-001", capture_ids=["capture-001"]), source_ids
        )


def test_only_an_explicit_knowledge_page_read_grants_revision_provenance() -> None:
    tool_event = Event(
        invocation_id="invocation-knowledge-tools",
        author="food_event_reasoner",
        content=types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name="list_household_knowledge",
                    response={
                        "pages": [
                            {
                                "page_id": "page-summary-only",
                                "current_revision_id": "revision-summary-only",
                            }
                        ]
                    },
                ),
                types.Part.from_function_response(
                    name="read_household_knowledge_page",
                    response={
                        "page": {
                            "page_id": "page-selected",
                            "revision": {"revision_id": "revision-selected"},
                        }
                    },
                ),
            ],
        ),
    )
    source_ids = _tool_context_source_ids(tool_event)
    assert source_ids[ContextSourceKind.HOUSEHOLD_KNOWLEDGE] == {"revision-selected"}

    payload = base_payload()
    payload["contextual_evidence"].append(
        {
            "id": "ctx_knowledge",
            "description": "The selected current household page supports steak.",
            "source_kind": "household_knowledge",
            "source_id": "revision-selected",
        }
    )
    payload["assumptions"] = [
        {
            "id": "asm_knowledge",
            "description": "The exact selected page says Thursday air-fryer meat is steak.",
            "knowledge_revision_id": "revision-selected",
        }
    ]
    inference = ActivityMealInferenceV1.model_validate(payload)
    bundle = event_bundle(
        event_id="event-001",
        capture_ids=["capture-001"],
        context={
            "recent_purchases": [{"purchase_id": "purchase-001"}],
            "household_knowledge": [],
            "recent_meals": [],
            "user_notes": [],
        },
    )
    _validate_source_identities(inference, bundle, source_ids)

    payload["contextual_evidence"][1]["source_id"] = "revision-summary-only"
    summary_only = ActivityMealInferenceV1.model_validate(payload)
    with pytest.raises(RuntimeError, match="invented household_knowledge source ID"):
        _validate_source_identities(summary_only, bundle, source_ids)


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
