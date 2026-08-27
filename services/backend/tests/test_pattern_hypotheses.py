from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.app import create_app
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    KnowledgeClaim,
    PatternEvidenceExample,
    QuestionEvidenceKind,
    QuestionEvidenceReference,
    QuestionResponseKind,
    QuestionResponseRequest,
)
from foodlog_backend.pattern_hypotheses import PatternHypothesisService
from foodlog_backend.settings import Settings

OWNER_HEADERS = {"X-FoodLog-Local-User": "pattern-owner"}
FOREIGN_HEADERS = {"X-FoodLog-Local-User": "pattern-foreign"}
START = datetime(2026, 8, 1, tzinfo=UTC)


def examples(prefix: str, count: int, *, start_index: int = 1) -> list[PatternEvidenceExample]:
    return [
        PatternEvidenceExample(
            evidence=QuestionEvidenceReference(
                kind=QuestionEvidenceKind.MEAL_REVISION,
                id=f"{prefix}-meal-revision-{index:03d}",
            ),
            occurred_at=START + timedelta(days=index),
            summary=f"Observed supporting {prefix} meal {index}.",
        )
        for index in range(start_index, start_index + count)
    ]


async def open_rich_pattern(
    repository,
    *,
    account_id: str,
    claim: KnowledgeClaim,
    statement: str,
    supports: list[PatternEvidenceExample],
    counters: list[PatternEvidenceExample] | None = None,
    ended_at: datetime | None = None,
):
    counterexamples = counters or []
    return await repository.open_pattern_question(
        account_id=account_id,
        prompt=f"I am noticing {statement}. Is that accurate?",
        reason="Multiple dated meal revisions support this longitudinal hypothesis.",
        tentative_claim=statement,
        evidence=[
            item.evidence for item in (*supports, *counterexamples)
        ],
        pattern_claim=claim,
        observation_started_at=START,
        observation_ended_at=ended_at or START + timedelta(days=21),
        supporting_examples=supports,
        counterexamples=counterexamples,
        prompt_version="pattern-hypothesis-v1",
    )


def test_rich_pattern_responses_create_scoped_knowledge_and_gate_resurfacing() -> None:
    app = create_app(Settings(environment="test"))
    repository = app.state.container.repository
    with TestClient(app) as client:
        owner = client.post("/v1/accounts", headers=OWNER_HEADERS).json()
        client.post("/v1/accounts", headers=FOREIGN_HEADERS)

        steak_claim = KnowledgeClaim(
            dimension="likely meal",
            value="steak",
            conditions=("Thursday dinner",),
        )
        steak_supports = examples("steak", 3)
        steak = asyncio.run(
            open_rich_pattern(
                repository,
                account_id=owner["id"],
                claim=steak_claim,
                statement="you usually eat steak for Thursday dinner",
                supports=steak_supports,
                counters=examples("steak-counter", 1),
            )
        )
        assert steak.pattern_claim == steak_claim
        assert steak.pattern_prompt_version == "pattern-hypothesis-v1"
        assert len(steak.pattern_supporting_examples) == 3
        assert len(steak.pattern_counterexamples) == 1

        pattern_feed = client.get(
            "/v1/questions?kind=pattern_hypothesis",
            headers=OWNER_HEADERS,
        )
        assert pattern_feed.status_code == 200
        assert pattern_feed.headers["cache-control"] == "private, no-store"
        assert [question["id"] for question in pattern_feed.json()] == [steak.id]
        invalid_kind = client.get(
            "/v1/questions?kind=meal_guess",
            headers=OWNER_HEADERS,
        )
        assert invalid_kind.status_code == 422

        request = {
            "headers": {**OWNER_HEADERS, "Idempotency-Key": "rich-pattern-confirm-0001"},
            "json": {"kind": "confirm", "explanation": "Thursday is our steak night."},
        }
        confirmed = client.post(f"/v1/questions/{steak.id}/responses", **request)
        confirmed_retry = client.post(f"/v1/questions/{steak.id}/responses", **request)
        assert confirmed.status_code == confirmed_retry.status_code == 200
        assert confirmed_retry.json() == confirmed.json()
        confirmed_knowledge = confirmed.json()["knowledge"]
        assert confirmed_knowledge["revision"]["claim"] == steak_claim.model_dump(
            mode="json"
        )
        assert confirmed_knowledge["revision"]["lifecycle"] == "confirmed"
        assert confirmed_knowledge["revision"]["belief_strength"] == "strong"
        assert confirmed_knowledge["revision"]["source"] == "question_response"

        breakfast_claim = KnowledgeClaim(
            dimension="weekday breakfast",
            value="cereal",
            conditions=("weekday",),
        )
        breakfast = asyncio.run(
            open_rich_pattern(
                repository,
                account_id=owner["id"],
                claim=breakfast_claim,
                statement="weekday breakfast is usually cereal",
                supports=examples("breakfast", 3),
            )
        )
        corrected_statement = "Weekday breakfast varies with my work schedule."
        corrected = client.post(
            f"/v1/questions/{breakfast.id}/responses",
            headers={
                **OWNER_HEADERS,
                "Idempotency-Key": "rich-pattern-correct-0001",
            },
            json={"kind": "correct", "correction": corrected_statement},
        )
        assert corrected.status_code == 200
        assert corrected.json()["knowledge"]["revision"]["statement"] == corrected_statement
        assert corrected.json()["knowledge"]["revision"]["claim"] is None

        pastry_claim = KnowledgeClaim(
            dimension="weekend breakfast",
            value="pastries",
            conditions=("weekend",),
        )
        original_supports = examples("pastry", 3)
        rejected = asyncio.run(
            open_rich_pattern(
                repository,
                account_id=owner["id"],
                claim=pastry_claim,
                statement="weekend breakfast is usually pastries",
                supports=original_supports,
            )
        )
        foreign = client.post(
            f"/v1/questions/{rejected.id}/responses",
            headers={
                **FOREIGN_HEADERS,
                "Idempotency-Key": "rich-pattern-foreign-0001",
            },
            json={"kind": "reject"},
        )
        assert foreign.status_code == 404
        response = client.post(
            f"/v1/questions/{rejected.id}/responses",
            headers={
                **OWNER_HEADERS,
                "Idempotency-Key": "rich-pattern-reject-0001",
            },
            json={"kind": "reject", "explanation": "That is not a real routine."},
        )
        assert response.status_code == 200
        assert response.json()["knowledge"] is None

        unchanged = asyncio.run(
            open_rich_pattern(
                repository,
                account_id=owner["id"],
                claim=pastry_claim,
                statement="weekend breakfast is usually pastries",
                supports=original_supports,
            )
        )
        one_new = asyncio.run(
            open_rich_pattern(
                repository,
                account_id=owner["id"],
                claim=pastry_claim,
                statement="weekend breakfast is usually pastries",
                supports=[*original_supports, *examples("pastry", 1, start_index=4)],
                ended_at=START + timedelta(days=28),
            )
        )
        assert unchanged.id == one_new.id == rejected.id

        resurfaced = asyncio.run(
            open_rich_pattern(
                repository,
                account_id=owner["id"],
                claim=pastry_claim,
                statement="weekend breakfast is usually pastries",
                supports=[*original_supports, *examples("pastry", 2, start_index=4)],
                ended_at=START + timedelta(days=35),
            )
        )
        assert resurfaced.id != rejected.id
        assert resurfaced.predecessor_question_id == rejected.id

        pages = client.get("/v1/knowledge", headers=OWNER_HEADERS).json()
        assert {page["statement"] for page in pages} >= {
            "you usually eat steak for Thursday dinner",
            corrected_statement,
        }


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_rich_pattern_response_persists_provenance_and_resurface_gate() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-rich-pattern-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner = await repository.provision_account("rich-pattern-owner")
        claim = KnowledgeClaim(
            dimension="likely meal",
            value="steak",
            conditions=("Thursday",),
        )
        supports = examples("firestore-steak", 3)
        question = await open_rich_pattern(
            repository,
            account_id=owner.id,
            claim=claim,
            statement="Thursday dinner is usually steak",
            supports=supports,
        )
        service = PatternHypothesisService(repository)
        request = QuestionResponseRequest(kind=QuestionResponseKind.CONFIRM)
        result = await service.respond(
            owner_user_id="rich-pattern-owner",
            question_id=question.id,
            request=request,
            idempotency_key="firestore-rich-pattern-confirm-0001",
        )
        retry = await service.respond(
            owner_user_id="rich-pattern-owner",
            question_id=question.id,
            request=request,
            idempotency_key="firestore-rich-pattern-confirm-0001",
        )
        assert retry == result
        assert result.knowledge is not None
        assert result.knowledge.revision.claim == claim
        assert result.knowledge.revision.evidence[0].id == result.response.id

        topic_snapshot = await (
            client.collection("accounts")
            .document(owner.id)
            .collection("pattern_hypothesis_topics")
            .document(question.pattern_topic_key)
            .get()
        )
        assert topic_snapshot.get("latest_question_id") == question.id
        client.close()

    asyncio.run(scenario())
