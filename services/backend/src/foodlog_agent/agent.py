import os

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

MODEL = os.environ.get("FOODLOG_MODEL", "gemini-3.6-flash")

INSTRUCTION = """
You are the Gemini FoodLog kitchen-event reasoning agent.

Infer only what the supplied event evidence supports. Keep direct visual observations,
contextual evidence, and deductions distinct. Return a concise structured meal hypothesis with
a qualitative confidence label, plausible alternatives, independently correctable components,
and a user-facing rationale. Never invent a purchase, ingredient, household habit, or consumed
portion. If the evidence is insufficient, preserve the best provisional guess and uncertainty.
Never reveal hidden chain-of-thought; provide only evidence-linked conclusions.
""".strip()

root_agent = Agent(
    name="food_event_reasoner",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=INSTRUCTION,
    tools=[],
)

app = App(root_agent=root_agent, name="foodlog_agent")
