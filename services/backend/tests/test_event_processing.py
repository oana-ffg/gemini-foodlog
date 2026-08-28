from __future__ import annotations

import asyncio
from datetime import timedelta
from hashlib import sha256
from typing import cast

import pytest

from foodlog_agent.event_processing import AdkEventReasoner, EventInferenceProcessor
from foodlog_agent.event_reasoning import (
    AccountedEventInference,
    InvalidModelOutputError,
)
from foodlog_backend.errors import QuestionSuperseded
from foodlog_backend.grouping import GroupingPolicy
from foodlog_backend.inference_schema import ActivityMealInferenceV1
from foodlog_backend.model_accounting import ModelInvocationExecutionError
from foodlog_backend.models import (
    ActivityEventStatus,
    CaptureEnvelopeV1,
    CaptureStatus,
    JobStatus,
    MealStatus,
    ModelSpendReservation,
    ModelUsageRecord,
    QuestionResponseKind,
    QuestionResponseRequest,
    QuestionStatus,
    capture_grouping_job_id,
    event_inference_job_id,
    utc_now,
)
from foodlog_backend.repository import InMemoryRepository
from tests.inference_fixtures import base_payload


class RecordingReasoner:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        result: AccountedEventInference | None = None,
    ) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.result = result or cast(AccountedEventInference, object())

    async def infer(self, **kwargs) -> AccountedEventInference:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class SequencedInvoker:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []
        self.result = cast(AccountedEventInference, object())

    async def __call__(self, **kwargs) -> AccountedEventInference:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if outcome == "invalid":
            try:
                raise InvalidModelOutputError("missing required best_guess")
            except InvalidModelOutputError as cause:
                raise ModelInvocationExecutionError(error_code="InvalidModelOutputError") from cause
        if outcome != "success":
            raise ModelInvocationExecutionError(error_code=outcome)
        return self.result


async def _store_and_group(
    repository: InMemoryRepository,
    *,
    account,
    camera,
    capture_id: str,
    captured_at,
    sequence_number: int,
):
    content = f"image-{capture_id}".encode()
    object_key = f"accounts/{account.id}/captures/{capture_id}.jpg"
    capture, _, created = await repository.reserve_capture(
        capture_id=capture_id,
        account=account,
        camera=camera,
        idempotency_key=f"idempotency-{capture_id}",
        content_type="image/jpeg",
        content_sha256=sha256(content).hexdigest(),
        object_key=object_key,
        metadata=CaptureEnvelopeV1(
            camera_id=camera.id,
            captured_at=captured_at,
            client_kind="browser",
            client_version="event-processing-test/1",
            sequence_id=f"sequence-{camera.id}",
            sequence_number=sequence_number,
            width=1280,
            height=720,
        ),
    )
    assert created is True
    await repository.mark_stored(account_id=account.id, capture_id=capture.id)
    lease_id = f"grouping-{capture.id}"
    claimed = await repository.claim_job(
        account_id=account.id,
        job_id=capture_grouping_job_id(capture.id),
        expected_subject_revision=1,
        lease_id=lease_id,
        lease_owner="grouping-test",
        lease_expires_at=utc_now() + timedelta(minutes=5),
    )
    assert claimed is not None
    grouped = await repository.group_capture(
        account_id=account.id,
        capture_id=capture.id,
        lease_id=lease_id,
        lease_owner="grouping-test",
        policy=GroupingPolicy(quiet_after=timedelta(microseconds=1)),
    )
    assert grouped is not None
    return grouped


async def _prepared_event():
    repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
    account = await repository.provision_account("event-processing-owner")
    sink = await repository.create_browser_camera(
        "event-processing-owner",
        "Sink",
        "event-processing-camera-sink",
    )
    stove = await repository.create_browser_camera(
        "event-processing-owner",
        "Stove",
        "event-processing-camera-stove",
    )
    now = utc_now()
    later = await _store_and_group(
        repository,
        account=account,
        camera=sink,
        capture_id="event-processing-later",
        captured_at=now - timedelta(minutes=1),
        sequence_number=1,
    )
    earlier = await _store_and_group(
        repository,
        account=account,
        camera=stove,
        capture_id="event-processing-earlier",
        captured_at=now - timedelta(minutes=2),
        sequence_number=0,
    )
    assert earlier.event.id == later.event.id
    return repository, account, earlier.event


def _accounted_hypothesis(
    *,
    event_id: str,
    capture_ids: list[str],
    kind: str = "tentative_meal",
    with_question: bool = False,
) -> AccountedEventInference:
    payload = base_payload()
    payload["event_id"] = event_id
    payload["source_capture_ids"] = capture_ids
    observation = payload["direct_observations"][0]
    observation["image_evidence"][0]["capture_id"] = capture_ids[0]
    payload["contextual_evidence"] = []
    payload["deductions"] = [
        {
            "id": "ded_meat",
            "description": "The visible food supports the current candidate.",
            "evidence_ids": ["obs_meat"],
        }
    ]
    payload["components"][0]["evidence_ids"] = ["obs_meat", "ded_meat"]
    payload["question"] = (
        {
            "prompt": "Was this beef steak or the lamb alternative?",
            "justification": "The visible red meat supports both named candidates.",
            "evidence_ids": ["obs_meat"],
            "candidate_labels": ["Air-fried steak", "Air-fried lamb"],
            "impact": "changes_meal_identity",
        }
        if with_question
        else None
    )
    if with_question:
        payload["confidence"] = "uncertain"
    if kind == "unknown_activity":
        payload.update(
            kind=kind,
            best_guess=None,
            confidence="uncertain",
            components=[],
            alternatives=[],
            allowed_actions=["correct", "discard_not_cooking"],
        )
    elif kind == "likely_non_cooking":
        payload.update(
            kind=kind,
            best_guess="Cat on the counter",
            confidence="likely",
            components=[],
            allowed_actions=["correct", "discard_not_cooking"],
        )
    inference = ActivityMealInferenceV1.model_validate(payload)
    return AccountedEventInference(
        inference=inference,
        reservation=cast(ModelSpendReservation, object()),
        usage=cast(ModelUsageRecord, object()),
    )


def test_claimed_revision_invokes_once_with_exact_ordered_bundle() -> None:
    async def scenario() -> None:
        repository, account, event = await _prepared_event()
        reasoner = RecordingReasoner()
        processor = EventInferenceProcessor(repository=repository, reasoner=reasoner)

        claimed = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=event.current_revision,
            worker_id="event-worker-1",
        )
        assert claimed is not None
        assert claimed.accounted is reasoner.result
        assert [capture.id for capture in claimed.captures] == [
            "event-processing-earlier",
            "event-processing-later",
        ]
        assert len(reasoner.calls) == 1
        call = reasoner.calls[0]
        assert call["event"] == claimed.event
        assert [capture.id for capture in call["captures"]] == [
            "event-processing-earlier",
            "event-processing-later",
        ]
        assert call["retry_attempt"] == 0
        assert call["evaluation"] is False
        assert call["invocation_key"] == (
            f"event:{event.id}:revision:{event.current_revision}:attempt:1"
        )

        duplicate = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=event.current_revision,
            worker_id="event-worker-2",
        )
        assert duplicate is None
        assert len(reasoner.calls) == 1

    asyncio.run(scenario())


def test_failed_inference_releases_the_exact_job_for_retry() -> None:
    async def scenario() -> None:
        repository, account, event = await _prepared_event()
        reasoner = RecordingReasoner(error=TimeoutError("provider timeout"))
        processor = EventInferenceProcessor(
            repository=repository,
            reasoner=reasoner,
            retry_delay=timedelta(0),
        )

        with pytest.raises(TimeoutError, match="provider timeout"):
            await processor.process(
                account_id=account.id,
                event_id=event.id,
                expected_revision=event.current_revision,
                worker_id="event-worker-failure",
            )

        job = await repository.job_for_account(
            account.id,
            event_inference_job_id(event.id),
        )
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.attempt_count == 1
        assert job.last_error_code == "TimeoutError"
        assert job.last_error_message == "provider timeout"

    asyncio.run(scenario())


def test_stale_revision_never_invokes_and_evaluation_can_release_success() -> None:
    async def scenario() -> None:
        repository, account, event = await _prepared_event()
        reasoner = RecordingReasoner()
        processor = EventInferenceProcessor(
            repository=repository,
            reasoner=reasoner,
            evaluation=True,
            purpose="deployment_smoke",
        )

        stale = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=event.current_revision + 1,
            worker_id="event-worker-stale",
            invocation_key="stale-evaluation-key",
        )
        assert stale is None
        assert reasoner.calls == []

        claimed = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=event.current_revision,
            worker_id="event-worker-evaluation",
            invocation_key="exact-evaluation-key",
        )
        assert claimed is not None
        assert await processor.release_evaluation(claimed) is True
        job = await repository.job_for_account(
            account.id,
            event_inference_job_id(event.id),
        )
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.last_error_code == "EvaluationComplete"

    asyncio.run(scenario())


def test_invalid_output_gets_one_separately_identified_repair_attempt() -> None:
    async def scenario() -> None:
        repository, _, event = await _prepared_event()
        _, captures = await repository.event_evidence_for_account(
            account_id=event.account_id,
            event_id=event.id,
        )
        invoker = SequencedInvoker(["invalid", "success"])
        reasoner = AdkEventReasoner(invoker=invoker)

        result = await reasoner.infer(
            repository=repository,
            event=event,
            captures=captures,
            invocation_key="bounded-repair",
            purpose="event_inference",
            retry_attempt=0,
            evaluation=False,
        )

        assert result is invoker.result
        assert len(invoker.calls) == 2
        assert invoker.calls[0]["invocation_key"] == "bounded-repair"
        assert invoker.calls[0]["retry_attempt"] == 0
        assert invoker.calls[0]["repair_feedback"] is None
        assert invoker.calls[1]["invocation_key"] == "bounded-repair:repair:1"
        assert invoker.calls[1]["retry_attempt"] == 1
        assert "missing required best_guess" in invoker.calls[1]["repair_feedback"]

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["TimeoutError", "RESOURCE_EXHAUSTED"])
def test_transport_and_quota_failures_do_not_trigger_immediate_repair(outcome: str) -> None:
    async def scenario() -> None:
        repository, _, event = await _prepared_event()
        _, captures = await repository.event_evidence_for_account(
            account_id=event.account_id,
            event_id=event.id,
        )
        invoker = SequencedInvoker([outcome])
        reasoner = AdkEventReasoner(invoker=invoker)

        with pytest.raises(ModelInvocationExecutionError) as raised:
            await reasoner.infer(
                repository=repository,
                event=event,
                captures=captures,
                invocation_key="no-immediate-repair",
                purpose="event_inference",
                retry_attempt=0,
                evaluation=False,
            )
        assert raised.value.error_code == outcome
        assert len(invoker.calls) == 1

    asyncio.run(scenario())


def test_exhausted_output_repair_remains_a_visible_failure() -> None:
    async def scenario() -> None:
        repository, account, event = await _prepared_event()
        invoker = SequencedInvoker(["invalid", "invalid"])
        processor = EventInferenceProcessor(
            repository=repository,
            reasoner=AdkEventReasoner(invoker=invoker),
            retry_delay=timedelta(0),
        )

        with pytest.raises(ModelInvocationExecutionError) as raised:
            await processor.process(
                account_id=account.id,
                event_id=event.id,
                expected_revision=event.current_revision,
                worker_id="event-worker-invalid-output",
            )
        assert raised.value.error_code == "InvalidModelOutputError"
        assert len(invoker.calls) == 2
        job = await repository.job_for_account(
            account.id,
            event_inference_job_id(event.id),
        )
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.failed_at is not None
        assert job.last_error_code == "InvalidModelOutputError"
        assert "missing required best_guess" in (job.last_error_message or "")

        duplicate = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=event.current_revision,
            worker_id="event-worker-invalid-output-redelivery",
        )
        assert duplicate is None
        assert len(invoker.calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "expected_title", "confirmable"),
    [
        ("tentative_meal", "Air-fried steak", True),
        ("unknown_activity", "Unknown kitchen activity", False),
        ("likely_non_cooking", "Cat on the counter", False),
    ],
)
def test_validated_hypothesis_is_published_once_with_its_distinct_state(
    kind: str,
    expected_title: str,
    confirmable: bool,
) -> None:
    async def scenario() -> None:
        repository, account, event = await _prepared_event()
        accounted = _accounted_hypothesis(
            event_id=event.id,
            capture_ids=["event-processing-earlier", "event-processing-later"],
            kind=kind,
        )
        reasoner = RecordingReasoner(result=accounted)
        processor = EventInferenceProcessor(repository=repository, reasoner=reasoner)

        claimed = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=event.current_revision,
            worker_id="publication-worker",
        )
        assert claimed is not None
        meal = await processor.publish(claimed)
        assert meal is not None
        assert meal.title == expected_title
        assert meal.status == MealStatus.PROVISIONAL
        assert meal.event_id == event.id
        assert meal.activity_hypothesis == accounted.inference
        assert ("confirm_guess" in meal.activity_hypothesis.allowed_actions) is confirmable

        stored_event, stored_captures = await repository.event_evidence_for_account(
            account_id=account.id,
            event_id=event.id,
        )
        assert stored_event.status == ActivityEventStatus.INFERRED
        assert stored_event.meal_id == meal.id
        assert all(capture.status == CaptureStatus.PROCESSED for capture in stored_captures)
        job = await repository.job_for_account(
            account.id,
            event_inference_job_id(event.id),
        )
        assert job is not None
        assert job.status == JobStatus.COMPLETED

        duplicate = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=event.current_revision,
            worker_id="duplicate-publication-worker",
        )
        assert duplicate is None
        assert len(reasoner.calls) == 1
        assert len(repository._meal_revisions[meal.id]) == 1

    asyncio.run(scenario())


def test_superseded_event_revision_cannot_publish_a_claimed_hypothesis() -> None:
    async def scenario() -> None:
        repository, account, event = await _prepared_event()
        accounted = _accounted_hypothesis(
            event_id=event.id,
            capture_ids=["event-processing-earlier", "event-processing-later"],
        )
        processor = EventInferenceProcessor(
            repository=repository,
            reasoner=RecordingReasoner(result=accounted),
        )
        claimed = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=event.current_revision,
            worker_id="stale-publication-worker",
        )
        assert claimed is not None

        await _store_and_group(
            repository,
            account=account,
            camera=await repository.camera_for_owner(
                account.owner_user_id,
                event.camera_ids[0],
            ),
            capture_id="event-processing-newer",
            captured_at=utc_now(),
            sequence_number=2,
        )
        assert await processor.publish(claimed) is None
        current_event, _ = await repository.event_evidence_for_account(
            account_id=account.id,
            event_id=event.id,
        )
        assert current_event.current_revision == event.current_revision + 1
        assert current_event.status == ActivityEventStatus.OPEN
        assert current_event.meal_id is None
        assert repository._meals == {}

    asyncio.run(scenario())


def test_reinference_supersedes_the_old_event_question_and_rejects_stale_response() -> None:
    async def scenario() -> None:
        repository, account, event = await _prepared_event()
        first = _accounted_hypothesis(
            event_id=event.id,
            capture_ids=["event-processing-earlier", "event-processing-later"],
            with_question=True,
        )
        processor = EventInferenceProcessor(
            repository=repository,
            reasoner=RecordingReasoner(result=first),
        )
        claimed = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=event.current_revision,
            worker_id="first-question-worker",
        )
        assert claimed is not None
        meal = await processor.publish(claimed)
        assert meal is not None
        old_question = (await repository.list_questions(account.owner_user_id))[0]
        assert old_question.status == QuestionStatus.OPEN
        assert old_question.choices == ["Air-fried steak", "Air-fried lamb"]

        await _store_and_group(
            repository,
            account=account,
            camera=await repository.camera_for_owner(
                account.owner_user_id,
                event.camera_ids[0],
            ),
            capture_id="event-processing-reopen-question",
            captured_at=utc_now(),
            sequence_number=2,
        )
        reopened, captures = await repository.event_evidence_for_account(
            account_id=account.id,
            event_id=event.id,
        )
        second = _accounted_hypothesis(
            event_id=event.id,
            capture_ids=[capture.id for capture in captures],
            with_question=True,
        )
        processor = EventInferenceProcessor(
            repository=repository,
            reasoner=RecordingReasoner(result=second),
        )
        claimed = await processor.process(
            account_id=account.id,
            event_id=event.id,
            expected_revision=reopened.current_revision,
            worker_id="replacement-question-worker",
        )
        assert claimed is not None
        revised_meal = await processor.publish(claimed)
        assert revised_meal is not None
        assert revised_meal.id == meal.id
        assert revised_meal.revision_number == 2

        questions = await repository.list_questions(
            account.owner_user_id,
            question_status=None,
        )
        replacement = next(item for item in questions if item.status == QuestionStatus.OPEN)
        superseded = next(item for item in questions if item.status == QuestionStatus.SUPERSEDED)
        assert superseded.id == old_question.id
        assert superseded.superseded_by_question_id == replacement.id
        with pytest.raises(QuestionSuperseded):
            await repository.respond_to_question(
                owner_user_id=account.owner_user_id,
                question_id=old_question.id,
                request=QuestionResponseRequest(kind=QuestionResponseKind.REJECT),
                idempotency_key="stale-event-question-response",
            )

    asyncio.run(scenario())
