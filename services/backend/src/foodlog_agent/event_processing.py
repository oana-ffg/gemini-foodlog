from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from foodlog_agent.event_reasoning import (
    AccountedEventInference,
    run_accounted_event_inference,
)
from foodlog_backend.model_accounting import ModelInvocationExecutionError
from foodlog_backend.models import (
    ActivityEvent,
    CaptureRecord,
    DurableJob,
    JobKind,
    MealEntry,
    event_inference_job_id,
    utc_now,
)
from foodlog_backend.repository import Repository


class EventReasoner(Protocol):
    async def infer(
        self,
        *,
        repository: Repository,
        event: ActivityEvent,
        captures: list[CaptureRecord],
        invocation_key: str,
        purpose: str,
        retry_attempt: int,
        evaluation: bool,
    ) -> AccountedEventInference: ...


class EventInferenceInvoker(Protocol):
    async def __call__(
        self,
        *,
        repository: Repository,
        event: ActivityEvent,
        captures: list[CaptureRecord],
        invocation_key: str,
        purpose: str,
        retry_attempt: int,
        evaluation: bool,
        repair_feedback: str | None = None,
    ) -> AccountedEventInference: ...


class AdkEventReasoner:
    def __init__(
        self,
        *,
        max_repair_attempts: int = 1,
        invoker: EventInferenceInvoker = run_accounted_event_inference,
    ) -> None:
        if not 0 <= max_repair_attempts <= 3:
            raise ValueError("max_repair_attempts must be between zero and three")
        self._max_repair_attempts = max_repair_attempts
        self._invoker = invoker

    async def infer(
        self,
        *,
        repository: Repository,
        event: ActivityEvent,
        captures: list[CaptureRecord],
        invocation_key: str,
        purpose: str,
        retry_attempt: int,
        evaluation: bool,
    ) -> AccountedEventInference:
        repair_feedback: str | None = None
        for repair_attempt in range(self._max_repair_attempts + 1):
            active_key = (
                invocation_key
                if repair_attempt == 0
                else f"{invocation_key}:repair:{repair_attempt}"
            )
            try:
                return await self._invoker(
                    repository=repository,
                    event=event,
                    captures=captures,
                    invocation_key=active_key,
                    purpose=purpose,
                    retry_attempt=retry_attempt + repair_attempt,
                    evaluation=evaluation,
                    repair_feedback=repair_feedback,
                )
            except ModelInvocationExecutionError as error:
                if (
                    error.error_code != "InvalidModelOutputError"
                    or repair_attempt >= self._max_repair_attempts
                ):
                    raise
                cause = error.__cause__
                repair_feedback = str(cause or error)[:1_200]
        raise AssertionError("bounded repair loop did not return or raise")


@dataclass(frozen=True)
class ClaimedEventInference:
    job: DurableJob
    lease_id: str
    lease_owner: str
    event: ActivityEvent
    captures: tuple[CaptureRecord, ...]
    accounted: AccountedEventInference


class EventInferenceProcessor:
    """Claim, reason over, and lease-fence one event-revision publication."""

    def __init__(
        self,
        *,
        repository: Repository,
        reasoner: EventReasoner,
        lease_duration: timedelta = timedelta(minutes=15),
        retry_delay: timedelta = timedelta(seconds=30),
        purpose: str = "event_inference",
        evaluation: bool = False,
    ) -> None:
        if lease_duration <= timedelta(0) or retry_delay < timedelta(0):
            raise ValueError("inference lease and retry durations are invalid")
        if not purpose or len(purpose) > 80:
            raise ValueError("inference purpose must contain 1 to 80 characters")
        self._repository = repository
        self._reasoner = reasoner
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._purpose = purpose
        self._evaluation = evaluation

    async def process(
        self,
        *,
        account_id: str,
        event_id: str,
        expected_revision: int,
        worker_id: str,
        invocation_key: str | None = None,
    ) -> ClaimedEventInference | None:
        if expected_revision < 1:
            raise ValueError("expected event revision must be positive")
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        if invocation_key is not None and not self._evaluation:
            raise ValueError("invocation key overrides are evaluation-only")

        lease_id = str(uuid4())
        job_id = event_inference_job_id(event_id)
        claimed = await self._repository.claim_job(
            account_id=account_id,
            job_id=job_id,
            expected_subject_revision=expected_revision,
            lease_id=lease_id,
            lease_owner=worker_id,
            lease_expires_at=utc_now() + self._lease_duration,
        )
        if claimed is None:
            return None

        try:
            if claimed.kind != JobKind.EVENT_INFERENCE or claimed.subject_id != event_id:
                raise RuntimeError("Claimed job is not the requested event inference")
            event, captures = await self._repository.event_evidence_for_account(
                account_id=account_id,
                event_id=event_id,
            )
            if event.current_revision != expected_revision:
                raise RuntimeError("Claimed event revision was superseded before inference")
            active_invocation_key = invocation_key or (
                f"event:{event.id}:revision:{expected_revision}:attempt:{claimed.attempt_count}"
            )
            accounted = await self._reasoner.infer(
                repository=self._repository,
                event=event,
                captures=captures,
                invocation_key=active_invocation_key,
                purpose=self._purpose,
                retry_attempt=claimed.attempt_count - 1,
                evaluation=self._evaluation,
            )
        except Exception as error:
            error_code = (
                error.error_code
                if isinstance(error, ModelInvocationExecutionError)
                else type(error).__name__
            )
            error_detail = str(error.__cause__ or error)
            await self._repository.release_job(
                account_id=account_id,
                job_id=job_id,
                expected_subject_revision=expected_revision,
                lease_id=lease_id,
                lease_owner=worker_id,
                available_at=utc_now() + self._retry_delay,
                error_code=error_code[:120],
                error_message=error_detail[:2_000] or error_code,
            )
            raise

        return ClaimedEventInference(
            job=claimed,
            lease_id=lease_id,
            lease_owner=worker_id,
            event=event,
            captures=tuple(captures),
            accounted=accounted,
        )

    async def publish(self, claimed: ClaimedEventInference) -> MealEntry | None:
        if self._evaluation:
            raise ValueError("Evaluation inference cannot publish a product result")
        return await self._repository.publish_event_inference(
            account_id=claimed.event.account_id,
            event_id=claimed.event.id,
            expected_event_revision=claimed.event.current_revision,
            lease_id=claimed.lease_id,
            lease_owner=claimed.lease_owner,
            hypothesis=claimed.accounted.inference,
        )

    async def release_evaluation(
        self,
        claimed: ClaimedEventInference,
        *,
        available_at: datetime | None = None,
    ) -> bool:
        if not self._evaluation:
            raise ValueError("Only evaluation processors may release a successful claim")
        return await self._repository.release_job(
            account_id=claimed.event.account_id,
            job_id=claimed.job.id,
            expected_subject_revision=claimed.event.current_revision,
            lease_id=claimed.lease_id,
            lease_owner=claimed.lease_owner,
            available_at=available_at or utc_now(),
            error_code="EvaluationComplete",
            error_message="Evaluation inference completed without publishing a product result.",
        )
