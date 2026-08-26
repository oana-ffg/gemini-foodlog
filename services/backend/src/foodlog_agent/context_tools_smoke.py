from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from foodlog_agent.context_tools import (
    ContextToolsService,
    production_context_tools_service,
)
from foodlog_agent.event_evidence_tool import ACCOUNT_ID_STATE_KEY
from foodlog_agent.knowledge_tools import (
    KnowledgeToolsService,
    production_knowledge_tools_service,
)


class SmokeContext:
    def __init__(self, *, account_id: str) -> None:
        self.state = {ACCOUNT_ID_STATE_KEY: account_id}


async def run_smoke(
    *,
    account_id: str,
    service: ContextToolsService | None = None,
    knowledge_service: KnowledgeToolsService | None = None,
) -> dict[str, Any]:
    context = SmokeContext(account_id=account_id)
    active_service = service or production_context_tools_service()
    recent = await active_service.get_recent_meals(context=context)
    active_context = await active_service.get_active_user_context(context=context)
    unresolved = await active_service.get_unresolved_reviews(context=context)
    active_knowledge_service = knowledge_service or production_knowledge_tools_service()
    knowledge_index = await active_knowledge_service.list_household_knowledge(
        context=context
    )
    selected_knowledge = None
    if knowledge_index.pages:
        selected_knowledge = (
            await active_knowledge_service.read_household_knowledge_page(
                page_id=knowledge_index.pages[0].page_id,
                context=context,
            )
        ).page
    return {
        "schema_version": recent.schema_version,
        "recent_meal_count": len(recent.meals),
        "recent_meal_ids": [meal.meal_id for meal in recent.meals],
        "recent_event_ids": [meal.event_id for meal in recent.meals],
        "active_note_count": len(active_context.notes),
        "active_note_ids": [note.note_id for note in active_context.notes],
        "unresolved_meal_count": len(unresolved.meals),
        "unresolved_meal_ids": [meal.meal_id for meal in unresolved.meals],
        "open_question_count": len(unresolved.questions),
        "open_question_ids": [question.question_id for question in unresolved.questions],
        "knowledge_page_count": len(knowledge_index.pages),
        "knowledge_page_ids": [page.page_id for page in knowledge_index.pages],
        "selected_knowledge_page_id": (
            selected_knowledge.page_id if selected_knowledge is not None else None
        ),
        "selected_knowledge_revision_id": (
            selected_knowledge.revision.revision_id
            if selected_knowledge is not None
            else None
        ),
        "model_calls": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read one account through the no-model agent context and wiki tools."
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument(
        "--confirm-private-read",
        action="store_true",
        help="Confirm this execution may read the selected account's private context.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_private_read:
        _parser().error("--confirm-private-read is required")
    print(
        json.dumps(
            asyncio.run(run_smoke(account_id=args.account_id)),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
