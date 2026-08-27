from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    CaptureEnvelopeV1,
    Confidence,
    KnowledgeClaim,
    MealEntry,
    QuestionResponseKind,
    QuestionResponseRequest,
)
from foodlog_backend.pattern_detection import PatternCandidate, PatternDetectionService
from foodlog_backend.repository import InMemoryRepository, Repository


async def seed_meal(
    repository: Repository,
    *,
    account_id: str,
    owner_user_id: str,
    camera_id: str,
    title: str,
    local_at: datetime,
) -> str:
    assert local_at.utcoffset() is not None
    capture_id = str(uuid4())
    account = await repository.account_for_owner(owner_user_id)
    camera = await repository.camera_for_owner(owner_user_id, camera_id)
    captured_at = local_at.astimezone(UTC)
    metadata = CaptureEnvelopeV1(
        camera_id=camera_id,
        captured_at=captured_at,
        client_kind="browser",
        client_version="pattern-test-v1",
        sequence_id=f"sequence-{capture_id}",
        sequence_number=1,
        width=640,
        height=480,
    )
    await repository.reserve_capture(
        capture_id=capture_id,
        account=account,
        camera=camera,
        idempotency_key=f"pattern-capture-{capture_id}",
        content_type="image/png",
        content_sha256="a" * 64,
        object_key=f"accounts/{account_id}/captures/{capture_id}.png",
        metadata=metadata,
    )
    meal = await repository.save_meal(
        account_id=account_id,
        meal=MealEntry(
            id=str(uuid4()),
            account_id=account_id,
            capture_id=capture_id,
            event_id=f"event-{capture_id}",
            occurred_at=captured_at,
            occurred_utc_offset_minutes=int(
                local_at.utcoffset().total_seconds() // 60
            ),
            title=title,
            confidence=Confidence.LIKELY,
            components=[],
            observations=[f"The retained event was labelled {title}."],
            alternatives=[],
            rationale="Synthetic dated pattern test evidence.",
        ),
    )
    evidence = await repository.recent_meal_evidence_for_account(
        account_id=account_id,
        limit=100,
    )
    return next(revision.id for stored, revision in evidence if stored.id == meal.id)


async def repository_with_owner() -> tuple[InMemoryRepository, str, str]:
    repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    account = await repository.provision_account("pattern-owner")
    camera = await repository.create_browser_camera(
        "pattern-owner",
        "Pattern camera",
        "pattern-camera-instance",
    )
    return repository, account.id, camera.id


def local_time(day: int, *, hour: int = 18) -> datetime:
    return datetime(
        2026,
        8,
        day,
        hour,
        tzinfo=timezone(timedelta(hours=2)),
    )


def test_detector_proposes_thursday_pattern_with_counterexample_and_deduplicates() -> None:
    async def scenario() -> None:
        repository, account_id, camera_id = await repository_with_owner()
        for day, title in ((6, "Steak"), (13, "Steak"), (20, "Steak"), (27, "Chicken")):
            await seed_meal(
                repository,
                account_id=account_id,
                owner_user_id="pattern-owner",
                camera_id=camera_id,
                title=title,
                local_at=local_time(day),
            )
        detector = PatternDetectionService(repository)
        first = await detector.detect_and_propose(account_id=account_id, max_proposals=5)
        duplicate = await detector.detect_and_propose(account_id=account_id, max_proposals=5)

        thursday = next(
            question
            for question in first
            if question.pattern_claim is not None
            and question.pattern_claim.conditions == ("thursday",)
        )
        assert thursday.pattern_claim.value == "steak"
        assert len(thursday.pattern_supporting_examples) == 3
        assert len(thursday.pattern_counterexamples) == 1
        assert thursday.pattern_uncertainty is not None
        assert [question.id for question in duplicate] == [question.id for question in first]

    asyncio.run(scenario())


def test_detector_uses_the_original_local_day_across_utc_midnight() -> None:
    async def scenario() -> None:
        repository, account_id, camera_id = await repository_with_owner()
        for day in (6, 13, 20):
            await seed_meal(
                repository,
                account_id=account_id,
                owner_user_id="pattern-owner",
                camera_id=camera_id,
                title="Steak",
                local_at=local_time(day, hour=0),
            )
        questions = await PatternDetectionService(repository).detect_and_propose(
            account_id=account_id,
            max_proposals=5,
        )
        thursday = next(
            question
            for question in questions
            if question.pattern_claim is not None
            and question.pattern_claim.conditions == ("thursday",)
        )
        assert all(
            example.occurred_at.weekday() == 2
            for example in thursday.pattern_supporting_examples
        )
        assert all(
            example.occurred_utc_offset_minutes == 120
            for example in thursday.pattern_supporting_examples
        )

    asyncio.run(scenario())


def test_rejected_pattern_requires_two_new_supports_before_resurfacing() -> None:
    async def scenario() -> None:
        repository, account_id, camera_id = await repository_with_owner()
        for day, title in ((6, "Steak"), (13, "Steak"), (20, "Steak"), (27, "Chicken")):
            await seed_meal(
                repository,
                account_id=account_id,
                owner_user_id="pattern-owner",
                camera_id=camera_id,
                title=title,
                local_at=local_time(day),
            )
        detector = PatternDetectionService(repository)
        original = next(
            question
            for question in await detector.detect_and_propose(
                account_id=account_id,
                max_proposals=5,
            )
            if question.pattern_claim is not None
            and question.pattern_claim.conditions == ("thursday",)
        )
        await repository.respond_to_question(
            owner_user_id="pattern-owner",
            question_id=original.id,
            request=QuestionResponseRequest(kind=QuestionResponseKind.REJECT),
            idempotency_key="detector-reject-0001",
        )
        await seed_meal(
            repository,
            account_id=account_id,
            owner_user_id="pattern-owner",
            camera_id=camera_id,
            title="Steak",
            local_at=datetime(
                2026,
                9,
                3,
                20,
                tzinfo=timezone(timedelta(hours=2)),
            ),
        )
        one_new = await detector.detect_and_propose(
            account_id=account_id,
            max_proposals=5,
        )
        assert not any(
            question.pattern_claim is not None
            and question.pattern_claim.conditions == ("thursday",)
            for question in one_new
        )

        await seed_meal(
            repository,
            account_id=account_id,
            owner_user_id="pattern-owner",
            camera_id=camera_id,
            title="Steak",
            local_at=datetime(
                2026,
                9,
                10,
                20,
                tzinfo=timezone(timedelta(hours=2)),
            ),
        )
        resurfaced = next(
            question
            for question in await detector.detect_and_propose(
                account_id=account_id,
                max_proposals=5,
            )
            if question.pattern_claim is not None
            and question.pattern_claim.conditions == ("thursday",)
        )
        assert resurfaced.id != original.id
        assert resurfaced.predecessor_question_id == original.id

    asyncio.run(scenario())


def test_detector_finds_weekday_and_weekend_breakfast_routines() -> None:
    async def scenario() -> None:
        repository, account_id, camera_id = await repository_with_owner()
        breakfast_rows = (
            (3, "Cereal"),
            (10, "Cereal"),
            (17, "Cereal"),
            (1, "Pancakes"),
            (8, "Pastries"),
            (15, "Pancakes"),
            (22, "Pastries"),
        )
        for day, title in breakfast_rows:
            await seed_meal(
                repository,
                account_id=account_id,
                owner_user_id="pattern-owner",
                camera_id=camera_id,
                title=title,
                local_at=local_time(day, hour=8),
            )
        detector = PatternDetectionService(repository)
        candidates = detector._candidate_cohorts(
            await repository.recent_meal_evidence_for_account(
                account_id=account_id,
                limit=100,
            )
        )
        claims = {(candidate.claim.value, candidate.claim.conditions) for candidate in candidates}
        assert ("cereal", ("breakfast", "weekday")) in claims
        assert ("pancakes or pastries", ("breakfast", "weekend")) in claims

    asyncio.run(scenario())


def test_sparse_unrelated_and_cross_account_evidence_never_create_a_pattern() -> None:
    async def scenario() -> None:
        repository, account_id, camera_id = await repository_with_owner()
        foreign = await repository.provision_account("pattern-foreign")
        foreign_camera = await repository.create_browser_camera(
            "pattern-foreign",
            "Foreign camera",
            "foreign-pattern-camera-instance",
        )
        for day in (6, 13):
            await seed_meal(
                repository,
                account_id=account_id,
                owner_user_id="pattern-owner",
                camera_id=camera_id,
                title="Steak",
                local_at=local_time(day),
            )
        for day in (6, 13, 20, 27):
            await seed_meal(
                repository,
                account_id=foreign.id,
                owner_user_id="pattern-foreign",
                camera_id=foreign_camera.id,
                title="Steak",
                local_at=local_time(day),
            )
        detector = PatternDetectionService(repository)
        assert await detector.detect_and_propose(account_id=account_id) == []

        candidate = PatternCandidate(
            statement="you usually eat steak on Thursdays",
            claim=KnowledgeClaim(
                dimension="likely meal",
                value="steak",
                conditions=("thursday",),
            ),
            supporting_revision_ids=("missing-a", "missing-b", "missing-c"),
            uncertainty="Synthetic unavailable evidence check.",
        )
        try:
            await detector.propose(account_id=account_id, candidate=candidate)
        except ValueError as error:
            assert str(error) == "pattern candidate cites unavailable meal revision evidence"
        else:
            raise AssertionError("unavailable or cross-account evidence was accepted")

    asyncio.run(scenario())


def test_detector_bounds_large_cohorts_to_the_question_evidence_limit() -> None:
    async def scenario() -> None:
        repository, account_id, camera_id = await repository_with_owner()
        first_thursday = datetime(
            2026,
            1,
            1,
            18,
            tzinfo=timezone(timedelta(hours=2)),
        )
        for week in range(24):
            await seed_meal(
                repository,
                account_id=account_id,
                owner_user_id="pattern-owner",
                camera_id=camera_id,
                title="Steak",
                local_at=first_thursday + timedelta(weeks=week),
            )
        questions = await PatternDetectionService(repository).detect_and_propose(
            account_id=account_id,
            max_proposals=5,
        )
        thursday = next(
            question
            for question in questions
            if question.pattern_claim is not None
            and question.pattern_claim.conditions == ("thursday",)
        )
        assert len(thursday.evidence) == 20
        assert len(thursday.pattern_supporting_examples) == 20

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_detector_persists_local_calendar_evidence_and_deduplicates() -> None:
    async def scenario() -> None:
        project_id = f"gemini-foodlog-pattern-detection-{uuid4().hex}"
        client = AsyncClient(project=project_id)
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            client=client,
        )
        owner_user_id = "firestore-pattern-owner"
        account = await repository.provision_account(owner_user_id)
        camera = await repository.create_browser_camera(
            owner_user_id,
            "Pattern camera",
            "firestore-pattern-camera-instance",
        )
        for day, title in ((6, "Steak"), (13, "Steak"), (20, "Steak"), (27, "Chicken")):
            await seed_meal(
                repository,
                account_id=account.id,
                owner_user_id=owner_user_id,
                camera_id=camera.id,
                title=title,
                local_at=local_time(day),
            )

        detector = PatternDetectionService(repository)
        first = await detector.detect_and_propose(
            account_id=account.id,
            max_proposals=5,
        )
        retry = await detector.detect_and_propose(
            account_id=account.id,
            max_proposals=5,
        )
        thursday = next(
            question
            for question in first
            if question.pattern_claim is not None
            and question.pattern_claim.conditions == ("thursday",)
        )
        assert [question.id for question in retry] == [question.id for question in first]
        assert {
            example.occurred_utc_offset_minutes
            for example in (
                *thursday.pattern_supporting_examples,
                *thursday.pattern_counterexamples,
            )
        } == {120}
        persisted = await repository.list_questions_for_owner(
            owner_user_id=owner_user_id,
            status=thursday.status,
            limit=20,
        )
        assert thursday.id in {question.id for question in persisted}
        client.close()

    asyncio.run(scenario())
