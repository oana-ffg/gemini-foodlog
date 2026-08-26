from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from google.adk.tools import FunctionTool, ToolContext
from pydantic import BaseModel, ConfigDict

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    KnowledgeBeliefStrength,
    KnowledgeClaim,
    KnowledgeEvidenceReference,
    KnowledgeLifecycle,
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeRevisionResult,
    KnowledgeRevisionSource,
)
from foodlog_backend.repository import Repository

from .event_evidence_tool import ACCOUNT_ID_STATE_KEY
from .session_state import SessionStateContext, required_state_identifier

KNOWLEDGE_TOOL_SCHEMA_VERSION = "agent-household-knowledge-v1"
KNOWLEDGE_TOOL_RESULT_LIMIT = 50


class HouseholdKnowledgePageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: str
    title: str
    lifecycle: KnowledgeLifecycle
    belief_strength: KnowledgeBeliefStrength
    current_revision_number: int
    updated_at: str


class HouseholdKnowledgeRevisionDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str
    revision_number: int
    source: KnowledgeRevisionSource
    evidence: list[KnowledgeEvidenceReference]
    reason: str
    created_at: str


class HouseholdKnowledgePageDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: str
    topic_key: str
    title: str
    statement: str
    claim: KnowledgeClaim | None
    lifecycle: KnowledgeLifecycle
    belief_strength: KnowledgeBeliefStrength
    revision: HouseholdKnowledgeRevisionDetail


class HouseholdKnowledgeIndexToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = KNOWLEDGE_TOOL_SCHEMA_VERSION
    pages: list[HouseholdKnowledgePageSummary]


class HouseholdKnowledgePageToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = KNOWLEDGE_TOOL_SCHEMA_VERSION
    page: HouseholdKnowledgePageDetail


def _page_summary(page: KnowledgePage) -> HouseholdKnowledgePageSummary:
    return HouseholdKnowledgePageSummary(
        page_id=page.id,
        title=page.title,
        lifecycle=page.lifecycle,
        belief_strength=page.belief_strength,
        current_revision_number=page.current_revision_number,
        updated_at=page.updated_at.isoformat(),
    )


def _revision_detail(revision: KnowledgeRevision) -> HouseholdKnowledgeRevisionDetail:
    return HouseholdKnowledgeRevisionDetail(
        revision_id=revision.id,
        revision_number=revision.number,
        source=revision.source,
        evidence=revision.evidence,
        reason=revision.reason,
        created_at=revision.created_at.isoformat(),
    )


def _page_detail(result: KnowledgeRevisionResult) -> HouseholdKnowledgePageDetail:
    return HouseholdKnowledgePageDetail(
        page_id=result.page.id,
        topic_key=result.page.topic_key,
        title=result.revision.title,
        statement=result.revision.statement,
        claim=result.revision.claim,
        lifecycle=result.revision.lifecycle,
        belief_strength=result.revision.belief_strength,
        revision=_revision_detail(result.revision),
    )


def _required_page_id(page_id: str) -> str:
    if not page_id or page_id != page_id.strip() or len(page_id) > 160:
        raise ValueError("page_id must contain 1-160 non-whitespace-padded characters")
    return page_id


class KnowledgeToolsService:
    def __init__(self, *, repository: Repository) -> None:
        self._repository = repository

    async def list_household_knowledge(
        self,
        *,
        context: SessionStateContext,
    ) -> HouseholdKnowledgeIndexToolResult:
        account_id = required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        pages = await self._repository.knowledge_page_index_for_account(
            account_id=account_id,
            limit=KNOWLEDGE_TOOL_RESULT_LIMIT,
        )
        return HouseholdKnowledgeIndexToolResult(
            pages=[_page_summary(page) for page in pages]
        )

    async def read_household_knowledge_page(
        self,
        *,
        page_id: str,
        context: SessionStateContext,
    ) -> HouseholdKnowledgePageToolResult:
        account_id = required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        result = await self._repository.active_knowledge_revision_for_account(
            account_id=account_id,
            page_id=_required_page_id(page_id),
        )
        return HouseholdKnowledgePageToolResult(page=_page_detail(result))


def build_knowledge_tools(*, repository: Repository) -> list[FunctionTool]:
    service = KnowledgeToolsService(repository=repository)

    async def list_household_knowledge(tool_context: ToolContext) -> dict[str, Any]:
        """List bounded household-wiki page summaries for the current account.

        Account scope comes only from trusted session state. Summaries are for selection only;
        read a relevant page before relying on or citing its current knowledge revision.
        """
        result = await service.list_household_knowledge(context=tool_context)
        return result.model_dump(mode="json")

    async def read_household_knowledge_page(
        page_id: str,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Read one selected current household-wiki page and its exact revision.

        Account scope comes only from trusted session state. The returned revision ID is the
        provenance identifier available for an assumption or household-knowledge citation.
        """
        result = await service.read_household_knowledge_page(
            page_id=page_id,
            context=tool_context,
        )
        return result.model_dump(mode="json")

    return [
        FunctionTool(func=list_household_knowledge),
        FunctionTool(func=read_household_knowledge_page),
    ]


@lru_cache(maxsize=1)
def production_knowledge_tools_service() -> KnowledgeToolsService:
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for knowledge tools")
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    return KnowledgeToolsService(repository=repository)


async def list_household_knowledge(tool_context: ToolContext) -> dict[str, Any]:
    """List bounded household-wiki page summaries for the current account.

    Account scope comes only from trusted session state. Read a selected page before using it
    as evidence; summary entries themselves are not valid inference provenance.
    """
    result = await production_knowledge_tools_service().list_household_knowledge(
        context=tool_context
    )
    return result.model_dump(mode="json")


async def read_household_knowledge_page(
    page_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Read one selected current household-wiki page for the current account.

    The returned revision ID is the only household-knowledge provenance made available by
    this read. Account scope comes only from trusted session state.
    """
    result = await production_knowledge_tools_service().read_household_knowledge_page(
        page_id=page_id,
        context=tool_context,
    )
    return result.model_dump(mode="json")


knowledge_tools = [
    FunctionTool(func=list_household_knowledge),
    FunctionTool(func=read_household_knowledge_page),
]
