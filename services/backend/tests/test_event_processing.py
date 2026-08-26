from __future__ import annotations

import asyncio
from datetime import timedelta
from hashlib import sha256
from typing import cast

import pytest

from foodlog_agent.event_processing import EventInferenceProcessor
from foodlog_agent.event_reasoning import AccountedEventInference
from foodlog_backend.grouping import GroupingPolicy
from foodlog_backend.models import (
    CaptureEnvelopeV1,
    JobStatus,
    capture_grouping_job_id,
    event_inference_job_id,
    utc_now,
)
from foodlog_backend.repository import InMemoryRepository


class RecordingReasoner:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.result = cast(AccountedEventInference, object())

    async def infer(self, **kwargs) -> AccountedEventInference:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
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
