from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from google.adk.tools import FunctionTool, ToolContext
from pydantic import BaseModel, ConfigDict, Field

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    ClarificationQuestion,
    Confidence,
    MealEntry,
    MealStatus,
    QuestionEvidenceReference,
    QuestionKind,
    UserContextNote,
)
from foodlog_backend.repository import Repository

from .event_evidence_tool import ACCOUNT_ID_STATE_KEY
from .session_state import SessionStateContext, required_state_identifier

CONTEXT_TOOL_SCHEMA_VERSION = "agent-context-v1"
CONTEXT_TOOL_RESULT_LIMIT = 20


class RecentMealSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_id: str
    event_id: str
    occurred_at: str
    title: str = Field(max_length=200)
    status: MealStatus
    confidence: Confidence
    revision_number: int = Field(ge=1)
    components: list[BoundedMealComponentSummary] = Field(max_length=20)
    observations: list[str] = Field(max_length=20)
    alternatives: list[str] = Field(max_length=8)
    rationale: str = Field(max_length=2_000)


class BoundedMealComponentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=160)
    ingredients: list[str] = Field(max_length=40)
    preparation_methods: list[str] = Field(max_length=20)


class ActiveUserContextSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note_id: str
    text: str
    created_at: str
    valid_from: str | None
    valid_until: str | None


class UnresolvedMealSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_id: str
    event_id: str | None
    occurred_at: str
    title: str = Field(max_length=200)
    status: MealStatus
    confidence: Confidence
    revision_number: int = Field(ge=1)
    rationale: str = Field(max_length=2_000)


class OpenQuestionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    kind: QuestionKind
    meal_id: str | None
    event_id: str | None
    prompt: str
    reason: str
    evidence: list[QuestionEvidenceReference]
    choices: list[str]
    tentative_claim: str | None
    created_at: str


class RecentMealsToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTEXT_TOOL_SCHEMA_VERSION
    meals: list[RecentMealSummary]


class ActiveUserContextToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTEXT_TOOL_SCHEMA_VERSION
    notes: list[ActiveUserContextSummary]


class UnresolvedReviewsToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTEXT_TOOL_SCHEMA_VERSION
    meals: list[UnresolvedMealSummary]
    questions: list[OpenQuestionSummary]


def _recent_meal_summary(meal: MealEntry) -> RecentMealSummary:
    if meal.event_id is None:
        raise ValueError("Recent agent meal context requires event provenance")
    return RecentMealSummary(
        meal_id=meal.id,
        event_id=meal.event_id,
        occurred_at=(meal.occurred_at or meal.created_at).isoformat(),
        title=meal.title[:200],
        status=meal.status,
        confidence=meal.confidence,
        revision_number=meal.revision_number,
        components=[
            BoundedMealComponentSummary(
                name=component.name[:160],
                ingredients=[item[:500] for item in component.ingredients[:40]],
                preparation_methods=[
                    item[:500] for item in component.preparation_methods[:20]
                ],
            )
            for component in meal.components[:20]
        ],
        observations=[item[:500] for item in meal.observations[:20]],
        alternatives=[item[:500] for item in meal.alternatives[:8]],
        rationale=meal.rationale[:2_000],
    )


def _active_context_summary(note: UserContextNote) -> ActiveUserContextSummary:
    return ActiveUserContextSummary(
        note_id=note.id,
        text=note.text,
        created_at=note.created_at.isoformat(),
        valid_from=note.valid_from.isoformat() if note.valid_from is not None else None,
        valid_until=note.valid_until.isoformat() if note.valid_until is not None else None,
    )


def _unresolved_meal_summary(meal: MealEntry) -> UnresolvedMealSummary:
    return UnresolvedMealSummary(
        meal_id=meal.id,
        event_id=meal.event_id,
        occurred_at=(meal.occurred_at or meal.created_at).isoformat(),
        title=meal.title[:200],
        status=meal.status,
        confidence=meal.confidence,
        revision_number=meal.revision_number,
        rationale=meal.rationale[:2_000],
    )


def _open_question_summary(question: ClarificationQuestion) -> OpenQuestionSummary:
    return OpenQuestionSummary(
        question_id=question.id,
        kind=question.kind,
        meal_id=question.meal_id,
        event_id=question.event_id,
        prompt=question.prompt,
        reason=question.reason,
        evidence=question.evidence,
        choices=question.choices,
        tentative_claim=question.tentative_claim,
        created_at=question.created_at.isoformat(),
    )


class ContextToolsService:
    def __init__(self, *, repository: Repository) -> None:
        self._repository = repository

    async def get_recent_meals(
        self,
        *,
        context: SessionStateContext,
    ) -> RecentMealsToolResult:
        account_id = required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        meals = await self._repository.recent_meals_for_account(
            account_id=account_id,
            limit=CONTEXT_TOOL_RESULT_LIMIT,
        )
        return RecentMealsToolResult(meals=[_recent_meal_summary(meal) for meal in meals])

    async def get_active_user_context(
        self,
        *,
        context: SessionStateContext,
    ) -> ActiveUserContextToolResult:
        account_id = required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        notes = await self._repository.active_user_context_notes_for_account(
            account_id=account_id,
            limit=CONTEXT_TOOL_RESULT_LIMIT,
        )
        return ActiveUserContextToolResult(
            notes=[_active_context_summary(note) for note in notes]
        )

    async def get_unresolved_reviews(
        self,
        *,
        context: SessionStateContext,
    ) -> UnresolvedReviewsToolResult:
        account_id = required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        meals, questions = await self._repository.unresolved_reviews_for_account(
            account_id=account_id,
            limit=CONTEXT_TOOL_RESULT_LIMIT,
        )
        return UnresolvedReviewsToolResult(
            meals=[_unresolved_meal_summary(meal) for meal in meals],
            questions=[_open_question_summary(question) for question in questions],
        )


def build_context_tools(*, repository: Repository) -> list[FunctionTool]:
    service = ContextToolsService(repository=repository)

    async def get_recent_meals(tool_context: ToolContext) -> dict[str, Any]:
        """Return bounded recent meal evidence for the current account.

        Account scope comes only from trusted session state. The result excludes activity
        discarded as not cooking and retains meal, event, revision, and timing provenance.
        """
        result = await service.get_recent_meals(context=tool_context)
        return result.model_dump(mode="json")

    async def get_active_user_context(tool_context: ToolContext) -> dict[str, Any]:
        """Return the current account's active time-bounded user notes.

        Account scope comes only from trusted session state. Future, expired, and retired
        notes are excluded; note IDs and validity windows remain visible as provenance.
        """
        result = await service.get_active_user_context(context=tool_context)
        return result.model_dump(mode="json")

    async def get_unresolved_reviews(tool_context: ToolContext) -> dict[str, Any]:
        """Return bounded unresolved meal and question evidence for the current account.

        Account scope comes only from trusted session state. Only provisional or contradicted
        meals and open questions are returned, with stable provenance identifiers.
        """
        result = await service.get_unresolved_reviews(context=tool_context)
        return result.model_dump(mode="json")

    return [
        FunctionTool(func=get_recent_meals),
        FunctionTool(func=get_active_user_context),
        FunctionTool(func=get_unresolved_reviews),
    ]


@lru_cache(maxsize=1)
def production_context_tools_service() -> ContextToolsService:
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for agent context tools")
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    return ContextToolsService(repository=repository)


async def get_recent_meals(tool_context: ToolContext) -> dict[str, Any]:
    """Return bounded recent meal evidence for the current account.

    Account scope comes only from trusted session state. Activity discarded as not cooking
    is excluded; returned event and revision IDs are the provenance available for citation.
    """
    result = await production_context_tools_service().get_recent_meals(
        context=tool_context
    )
    return result.model_dump(mode="json")


async def get_active_user_context(tool_context: ToolContext) -> dict[str, Any]:
    """Return the current account's active time-bounded user notes.

    Account scope comes only from trusted session state. Future, expired, and retired notes
    are excluded; returned note IDs and validity windows are the provenance for citation.
    """
    result = await production_context_tools_service().get_active_user_context(
        context=tool_context
    )
    return result.model_dump(mode="json")


async def get_unresolved_reviews(tool_context: ToolContext) -> dict[str, Any]:
    """Return unresolved meal and question evidence for the current account.

    Account scope comes only from trusted session state. Only provisional or contradicted
    meals and open questions are returned, with stable provenance identifiers.
    """
    result = await production_context_tools_service().get_unresolved_reviews(
        context=tool_context
    )
    return result.model_dump(mode="json")


context_tools = [
    FunctionTool(func=get_recent_meals),
    FunctionTool(func=get_active_user_context),
    FunctionTool(func=get_unresolved_reviews),
]
