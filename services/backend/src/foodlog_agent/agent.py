import os

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import load_artifacts
from google.genai import types

from foodlog_agent.context_tools import context_tools
from foodlog_agent.event_evidence_tool import event_evidence_tool
from foodlog_agent.prompt import INSTRUCTION
from foodlog_backend.inference_schema import ActivityMealInferenceModelOutputV1
from foodlog_backend.model_probe import DEFAULT_MODEL

MODEL = os.environ.get("FOODLOG_MODEL", DEFAULT_MODEL)
MAX_PROVIDER_ATTEMPTS = 1

root_agent = Agent(
    name="food_event_reasoner",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=MAX_PROVIDER_ATTEMPTS),
    ),
    instruction=INSTRUCTION,
    tools=[event_evidence_tool, *context_tools, load_artifacts],
    output_schema=ActivityMealInferenceModelOutputV1,
    generate_content_config=types.GenerateContentConfig(
        max_output_tokens=2_048,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.MINIMAL,
        ),
    ),
)

app = App(root_agent=root_agent, name="foodlog_agent")
