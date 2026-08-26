import asyncio

import pytest

from foodlog_agent.context_tools import ContextToolsService
from foodlog_agent.context_tools_smoke import run_smoke
from foodlog_agent.event_evidence_tool import ACCOUNT_ID_STATE_KEY
from foodlog_agent.knowledge_tools import (
    KNOWLEDGE_TOOL_RESULT_LIMIT,
    KNOWLEDGE_TOOL_SCHEMA_VERSION,
    KnowledgeToolsService,
    build_knowledge_tools,
)
from foodlog_backend.errors import KnowledgePageNotFound
from foodlog_backend.models import (
    KnowledgeBeliefStrength,
    KnowledgeClaim,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeLifecycle,
    KnowledgeRevisionDraft,
    KnowledgeRevisionSource,
)
from foodlog_backend.repository import InMemoryRepository


class StateContext:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state


def _draft(*, title: str, statement: str) -> KnowledgeRevisionDraft:
    return KnowledgeRevisionDraft(
        title=title,
        statement=statement,
        claim=KnowledgeClaim(
            dimension="likely meal",
            value="steak",
            conditions=("thursday", "air fryer"),
        ),
        lifecycle=KnowledgeLifecycle.CONFIRMED,
        belief_strength=KnowledgeBeliefStrength.STRONG,
        source=KnowledgeRevisionSource.USER_FEEDBACK,
        evidence=[
            KnowledgeEvidenceReference(
                kind=KnowledgeEvidenceKind.FEEDBACK,
                id=f"feedback-{title.casefold().replace(' ', '-')}",
                role=KnowledgeEvidenceRole.SUPPORTS,
            )
        ],
        reason="The user explicitly confirmed this narrow household pattern.",
    )


def test_knowledge_tools_select_then_read_exact_current_tenant_page() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        owner = await repository.provision_account("knowledge-tool-owner")
        foreign = await repository.provision_account("knowledge-tool-foreign")
        older = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key="weekday breakfast",
            expected_revision_number=None,
            draft=_draft(
                title="Weekday breakfast",
                statement="Cereal is usually eaten for weekday breakfast.",
            ),
            idempotency_key="knowledge-tool-weekday-breakfast",
        )
        current = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key="thursday air fryer dinner",
            expected_revision_number=None,
            draft=_draft(
                title="Thursday air-fryer dinner",
                statement="Steak is usually cooked in the air fryer on Thursdays.",
            ),
            idempotency_key="knowledge-tool-thursday-dinner",
        )
        foreign_page = await repository.record_knowledge_revision(
            account_id=foreign.id,
            topic_key="foreign private pattern",
            expected_revision_number=None,
            draft=_draft(
                title="Foreign private pattern",
                statement="This statement belongs only to another account.",
            ),
            idempotency_key="knowledge-tool-foreign-pattern",
        )

        tools = {tool.name: tool for tool in build_knowledge_tools(repository=repository)}
        owner_context = StateContext({ACCOUNT_ID_STATE_KEY: owner.id})
        index = await tools["list_household_knowledge"].run_async(  # type: ignore[arg-type]
            args={},
            tool_context=owner_context,
        )
        assert index["schema_version"] == KNOWLEDGE_TOOL_SCHEMA_VERSION
        assert [page["page_id"] for page in index["pages"]] == [
            current.page.id,
            older.page.id,
        ]
        assert len(index["pages"]) <= KNOWLEDGE_TOOL_RESULT_LIMIT
        assert all("statement" not in page for page in index["pages"])
        assert all("revision_id" not in page for page in index["pages"])

        selected = await tools["read_household_knowledge_page"].run_async(  # type: ignore[arg-type]
            args={"page_id": current.page.id},
            tool_context=owner_context,
        )
        assert selected["page"]["statement"] == current.revision.statement
        assert selected["page"]["revision"]["revision_id"] == current.revision.id
        assert selected["page"]["revision"]["revision_number"] == 1
        serialized = repr((index, selected))
        assert owner.id not in serialized
        assert foreign.id not in serialized
        assert "account_id" not in serialized
        assert foreign_page.page.id not in serialized

        smoke = await run_smoke(
            account_id=owner.id,
            service=ContextToolsService(repository=repository),
            knowledge_service=KnowledgeToolsService(repository=repository),
        )
        assert smoke["knowledge_page_ids"] == [current.page.id, older.page.id]
        assert smoke["selected_knowledge_page_id"] == current.page.id
        assert smoke["selected_knowledge_revision_id"] == current.revision.id
        assert smoke["model_calls"] == 0

        foreign_context = StateContext({ACCOUNT_ID_STATE_KEY: foreign.id})
        foreign_index = await tools["list_household_knowledge"].run_async(  # type: ignore[arg-type]
            args={},
            tool_context=foreign_context,
        )
        assert [page["page_id"] for page in foreign_index["pages"]] == [
            foreign_page.page.id
        ]
        with pytest.raises(KnowledgePageNotFound):
            await tools["read_household_knowledge_page"].run_async(  # type: ignore[arg-type]
                args={"page_id": current.page.id},
                tool_context=foreign_context,
            )
        with pytest.raises(KnowledgePageNotFound):
            await tools["read_household_knowledge_page"].run_async(  # type: ignore[arg-type]
                args={"page_id": foreign_page.page.id},
                tool_context=owner_context,
            )

    asyncio.run(scenario())


def test_knowledge_tool_declarations_expose_only_page_selection() -> None:
    tools = build_knowledge_tools(
        repository=InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    )
    assert [tool.name for tool in tools] == [
        "list_household_knowledge",
        "read_household_knowledge_page",
    ]
    list_declaration = tools[0]._get_declaration().model_dump(
        mode="json", exclude_none=True
    )
    assert "parameters" not in list_declaration
    assert "parameters_json_schema" not in list_declaration
    read_declaration = tools[1]._get_declaration().model_dump(
        mode="json", exclude_none=True
    )
    schema = read_declaration["parameters_json_schema"]
    assert set(schema["properties"]) == {"page_id"}
    assert schema["required"] == ["page_id"]


@pytest.mark.parametrize(
    "state",
    [{}, {ACCOUNT_ID_STATE_KEY: " account"}, {ACCOUNT_ID_STATE_KEY: ""}],
)
def test_knowledge_tools_require_valid_trusted_account_state(
    state: dict[str, object],
) -> None:
    async def scenario() -> None:
        tools = build_knowledge_tools(
            repository=InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        )
        with pytest.raises(ValueError, match="valid account_id"):
            await tools[0].run_async(  # type: ignore[arg-type]
                args={},
                tool_context=StateContext(state),
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("page_id", ["", " padded", "padded ", "x" * 161])
def test_knowledge_page_read_rejects_invalid_model_selected_ids(page_id: str) -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        owner = await repository.provision_account("knowledge-tool-invalid-page-owner")
        tools = build_knowledge_tools(repository=repository)
        with pytest.raises(ValueError, match="page_id"):
            await tools[1].run_async(  # type: ignore[arg-type]
                args={"page_id": page_id},
                tool_context=StateContext({ACCOUNT_ID_STATE_KEY: owner.id}),
            )

    asyncio.run(scenario())
