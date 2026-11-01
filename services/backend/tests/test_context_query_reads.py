import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.errors import AccountNotProvisioned, CrossAccountAccess
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    ClarificationQuestion,
    Confidence,
    MealEntry,
    MealStatus,
    QuestionStatus,
)


class CountingQuery:
    """Apply server query operations before counting returned document snapshots."""

    def __init__(self, documents: list, reads: list[str]):
        self.documents = documents
        self.reads = reads

    def where(self, *, filter):
        def matches(document):
            value = getattr(document, filter.field_path)
            if filter.op_string == "==":
                return value == filter.value
            assert filter.op_string == "in"
            return value in filter.value

        return CountingQuery([item for item in self.documents if matches(item)], self.reads)

    def order_by(self, field, *, direction):
        key = "id" if field == "__name__" else field
        # Each order_by adds a lower-priority ordering; stable-sort from last to first.
        query = CountingQuery(self.documents, self.reads)
        query._ordering = [*getattr(self, "_ordering", []), (key, direction)]
        return query

    def limit(self, count):
        documents = self.documents
        for field, direction in reversed(getattr(self, "_ordering", [])):
            documents = sorted(
                documents,
                key=lambda item: getattr(item, field),
                reverse=direction == firestore.Query.DESCENDING,
            )
        return CountingQuery(documents[:count], self.reads)

    async def stream(self):
        for document in self.documents:
            self.reads.append(document.id)
            yield SimpleNamespace(to_dict=lambda item=document: item.model_dump())


def history():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    meals = [
        MealEntry(
            id=f"meal-{index:03}",
            account_id="account-a",
            capture_id=f"capture-{index}",
            event_id=f"event-{index}",
            title="Synthetic meal",
            confidence=Confidence.LIKELY,
            components=[],
            observations=[],
            alternatives=[],
            rationale="Synthetic evidence.",
            # Exercise the existing created_at fallback and ID tie-breaking.
            occurred_at=None if index % 2 else now + timedelta(hours=index),
            created_at=now + timedelta(hours=index),
            status=(
                MealStatus.PROVISIONAL
                if index < 5
                else MealStatus.CONTRADICTED
                if index < 10
                else MealStatus.CONFIRMED
            ),
        )
        for index in range(180)
    ]
    questions = [
        ClarificationQuestion(
            id=f"question-{index:03}",
            account_id="account-a",
            meal_id=f"meal-{index:03}",
            prompt="Was this chicken?",
            reason="Synthetic ingredient ambiguity.",
            created_at=now + timedelta(hours=index // 2),
            status=QuestionStatus.OPEN if index < 30 else QuestionStatus.ANSWERED,
            answer="Yes" if index >= 30 else None,
            answered_at=now + timedelta(days=2) if index >= 30 else None,
        )
        for index in range(60)
    ]
    return meals, questions


@pytest.mark.parametrize("limit", [3, 20])
def test_unresolved_reviews_reads_only_eligible_history(monkeypatch, limit):
    async def scenario():
        repository = FirestoreRepository(
            project_id="offline-query-test",
            public_account_limit=25,
            trial_image_limit=200,
            client=SimpleNamespace(),
        )
        account = SimpleNamespace(exists=True, get=lambda field: "active")
        monkeypatch.setattr(
            repository,
            "_account",
            lambda _: SimpleNamespace(
                get=AsyncMock(
                    return_value=account,
                )
            ),
        )
        meals, questions = history()
        reads: list[str] = []
        collections = {"meals": meals, "questions": questions}
        monkeypatch.setattr(
            repository,
            "_collection",
            lambda account_id, name: CountingQuery(
                collections[name],
                reads,
            ),
        )
        actual_meals, actual_questions = await repository.unresolved_reviews_for_account(
            account_id="account-a",
            limit=limit,
        )
        assert (
            actual_meals
            == sorted(
                meals[:10],
                key=lambda item: (item.occurred_at or item.created_at, item.id),
                reverse=True,
            )[:limit]
        )
        assert (
            actual_questions
            == sorted(
                questions[:30],
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )[:limit]
        )
        assert len(reads) == 10 + limit, f"returned document snapshots: {len(reads)}"

        account.exists = False
        with pytest.raises(AccountNotProvisioned):
            await repository.unresolved_reviews_for_account(account_id="account-a", limit=limit)
        assert len(reads) == 10 + limit

        account.exists = True
        collections["meals"] = [meals[0].model_copy(update={"account_id": "foreign"})]
        with pytest.raises(CrossAccountAccess):
            await repository.unresolved_reviews_for_account(account_id="account-a", limit=limit)

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ, reason="requires Firestore emulator"
)
def test_unresolved_review_query_matches_firestore_order_and_limit():
    async def scenario():
        project_id = f"gemini-foodlog-context-query-{uuid4().hex}"
        client = AsyncClient(project=project_id, credentials=AnonymousCredentials())
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        account_ref = client.collection("accounts").document("account-a")
        meals, questions = history()
        batch = client.batch()
        batch.set(account_ref, {"status": "active"})
        for name, documents in (("meals", meals), ("questions", questions)):
            for document in documents:
                batch.set(account_ref.collection(name).document(document.id), document.model_dump())
        await batch.commit()
        try:
            actual_meals, actual_questions = await repository.unresolved_reviews_for_account(
                account_id="account-a",
                limit=20,
            )
            assert actual_meals == sorted(
                meals[:10],
                key=lambda item: (item.occurred_at or item.created_at, item.id),
                reverse=True,
            )
            assert (
                actual_questions
                == sorted(
                    questions[:30],
                    key=lambda item: (item.created_at, item.id),
                    reverse=True,
                )[:20]
            )
        finally:
            client.close()

    asyncio.run(scenario())
