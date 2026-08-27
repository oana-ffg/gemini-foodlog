from foodlog_agent import app, root_agent
from foodlog_agent.agent import MAX_PROVIDER_ATTEMPTS
from foodlog_agent.prompt import INSTRUCTION, PROMPT_VERSION
from foodlog_backend.inference_schema import ActivityMealInferenceModelOutputV1


def test_adk_agent_definition_is_importable_without_calling_a_model() -> None:
    assert app.name == "foodlog_agent"
    assert root_agent.name == "food_event_reasoner"
    assert root_agent.model.model == "gemini-3.6-flash"
    assert root_agent.output_schema is ActivityMealInferenceModelOutputV1
    assert root_agent.instruction == INSTRUCTION
    assert PROMPT_VERSION == "food-event-v9"
    assert root_agent.generate_content_config.max_output_tokens == 2_048
    assert MAX_PROVIDER_ATTEMPTS == 1
    assert root_agent.model.retry_options.attempts == 1
    assert [tool.name for tool in root_agent.tools] == [
        "get_current_event_evidence",
        "get_recent_meals",
        "get_recent_purchases",
        "get_active_user_context",
        "get_unresolved_reviews",
        "list_household_knowledge",
        "read_household_knowledge_page",
        "load_artifacts",
    ]
