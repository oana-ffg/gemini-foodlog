from google.adk.events import Event
from google.genai import types

from foodlog_agent.inference_schema import ActivityMealInferenceV1
from foodlog_agent.prompt import PROMPT_VERSION
from foodlog_agent.smoke import _structured_response, main, smoke_event_bundle
from tests.inference_fixtures import base_payload


def test_smoke_bundle_records_prompt_and_exact_source_identities() -> None:
    bundle = smoke_event_bundle()
    assert bundle["prompt_version"] == PROMPT_VERSION
    assert bundle["event"]["event_id"] == "adk-smoke-event-v1"
    assert bundle["event"]["captures"][0]["capture_id"] == "adk-smoke-capture-v1"


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


def test_cli_refuses_an_accidental_billable_invocation() -> None:
    try:
        main([])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("the smoke command accepted an unconfirmed billable invocation")
