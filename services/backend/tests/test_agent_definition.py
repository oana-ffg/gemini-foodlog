from foodlog_agent import app, root_agent


def test_adk_agent_definition_is_importable_without_calling_a_model() -> None:
    assert app.name == "foodlog_agent"
    assert root_agent.name == "food_event_reasoner"
    assert root_agent.model.model == "gemini-3.6-flash"
