from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from .errors import ModelInvocationAlreadyReconciled
from .models import ModelSpendReservation, ModelUsageRecord
from .operational_logging import emit_operational_event

CONSERVATIVE_DKK_PER_USD = 8
MODEL_INPUT_USD_NANOS_PER_TOKEN = {"gemini-3.6-flash": 825}
MODEL_OUTPUT_USD_NANOS_PER_TOKEN = {"gemini-3.6-flash": 4_125}


class ModelAccountingRepository(Protocol):
    async def reserve_model_spend(
        self,
        reservation: ModelSpendReservation,
    ) -> ModelSpendReservation: ...

    async def model_usage_for_reservation(
        self,
        *,
        account_id: str,
        reservation_id: str,
    ) -> ModelUsageRecord | None: ...

    async def record_model_usage(self, usage: ModelUsageRecord) -> ModelUsageRecord: ...


@dataclass(frozen=True)
class ModelInvocationSpec:
    invocation_key: str
    account_id: str
    event_id: str
    model: str
    region: str
    purpose: str
    prompt_version: str | None
    max_prompt_tokens: int
    max_output_tokens: int
    max_billable_calls: int
    retry_attempt: int
    evaluation: bool

    def __post_init__(self) -> None:
        for field_name in ("invocation_key", "account_id", "event_id", "region", "purpose"):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} is required")
        if self.model not in MODEL_INPUT_USD_NANOS_PER_TOKEN:
            raise ValueError(f"No reviewed pricing is configured for model {self.model}")
        if (
            min(
                self.max_prompt_tokens,
                self.max_output_tokens,
                self.max_billable_calls,
            )
            <= 0
        ):
            raise ValueError("model invocation ceilings and billable calls must be positive")
        if self.retry_attempt < 0:
            raise ValueError("retry_attempt cannot be negative")


@dataclass(frozen=True)
class CompletedModelInvocation[ResultT]:
    result: ResultT
    invocation_id: str
    model_version: str | None
    prompt_tokens: int
    response_tokens: int
    thinking_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class AccountedModelInvocation[ResultT]:
    result: ResultT
    reservation: ModelSpendReservation
    usage: ModelUsageRecord


class ModelInvocationExecutionError(Exception):
    def __init__(
        self,
        *,
        error_code: str,
        invocation_id: str | None = None,
        model_version: str | None = None,
        prompt_tokens: int = 0,
        response_tokens: int = 0,
        thinking_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.invocation_id = invocation_id
        self.model_version = model_version
        self.prompt_tokens = prompt_tokens
        self.response_tokens = response_tokens
        self.thinking_tokens = thinking_tokens
        self.total_tokens = total_tokens


def usd_nanos_to_conservative_dkk_micros(usd_nanos: int) -> int:
    if usd_nanos < 0:
        raise ValueError("USD nanos cannot be negative")
    return (usd_nanos * CONSERVATIVE_DKK_PER_USD + 999) // 1_000


def model_cost_usd_nanos(
    *,
    model: str,
    prompt_tokens: int,
    response_tokens: int,
    thinking_tokens: int,
) -> int:
    if min(prompt_tokens, response_tokens, thinking_tokens) < 0:
        raise ValueError("model token counts cannot be negative")
    try:
        input_rate = MODEL_INPUT_USD_NANOS_PER_TOKEN[model]
        output_rate = MODEL_OUTPUT_USD_NANOS_PER_TOKEN[model]
    except KeyError as error:
        raise ValueError(f"No reviewed pricing is configured for model {model}") from error
    return prompt_tokens * input_rate + (response_tokens + thinking_tokens) * output_rate


def conservative_reservation_dkk_micros(spec: ModelInvocationSpec) -> int:
    per_attempt_usd_nanos = model_cost_usd_nanos(
        model=spec.model,
        prompt_tokens=spec.max_prompt_tokens,
        response_tokens=spec.max_output_tokens,
        thinking_tokens=0,
    )
    return usd_nanos_to_conservative_dkk_micros(per_attempt_usd_nanos * spec.max_billable_calls)


def reservation_id_for_invocation(spec: ModelInvocationSpec) -> str:
    identity = "\0".join(
        (
            spec.account_id,
            spec.event_id,
            spec.purpose,
            str(spec.retry_attempt),
            spec.invocation_key,
        )
    )
    return f"model-{sha256(identity.encode()).hexdigest()}"


def _emit_model_usage(spec: ModelInvocationSpec, usage: ModelUsageRecord) -> None:
    emit_operational_event(
        "INFO" if usage.outcome == "succeeded" else "WARNING",
        "model_usage_recorded",
        service="model_accounting",
        account_id=spec.account_id,
        event_id=spec.event_id,
        model=spec.model,
        purpose=spec.purpose,
        workload="evaluation" if spec.evaluation else "production",
        outcome=usage.outcome,
        retry_attempt=usage.retry_attempt,
        total_tokens=usage.total_tokens,
        actual_dkk_micros=usage.actual_dkk_micros,
    )


async def execute_accounted_model_invocation[ResultT](
    *,
    repository: ModelAccountingRepository,
    spec: ModelInvocationSpec,
    invoke: Callable[[], Awaitable[CompletedModelInvocation[ResultT]]],
) -> AccountedModelInvocation[ResultT]:
    reservation = ModelSpendReservation(
        id=reservation_id_for_invocation(spec),
        account_id=spec.account_id,
        event_id=spec.event_id,
        reserved_dkk_micros=conservative_reservation_dkk_micros(spec),
        model=spec.model,
        region=spec.region,
        purpose=spec.purpose,
        prompt_version=spec.prompt_version,
        max_prompt_tokens=spec.max_prompt_tokens,
        max_output_tokens=spec.max_output_tokens,
        max_billable_calls=spec.max_billable_calls,
        retry_attempt=spec.retry_attempt,
        evaluation=spec.evaluation,
    )
    reservation = await repository.reserve_model_spend(reservation)
    existing = await repository.model_usage_for_reservation(
        account_id=spec.account_id,
        reservation_id=reservation.id,
    )
    if existing is not None:
        raise ModelInvocationAlreadyReconciled

    try:
        completed = await invoke()
    except Exception as error:
        partial = error if isinstance(error, ModelInvocationExecutionError) else None
        prompt_tokens = partial.prompt_tokens if partial is not None else 0
        response_tokens = partial.response_tokens if partial is not None else 0
        thinking_tokens = partial.thinking_tokens if partial is not None else 0
        total_tokens = partial.total_tokens if partial is not None else 0
        actual_usd_nanos = model_cost_usd_nanos(
            model=spec.model,
            prompt_tokens=prompt_tokens,
            response_tokens=response_tokens,
            thinking_tokens=thinking_tokens,
        )
        usage = await repository.record_model_usage(
            ModelUsageRecord(
                id=reservation.id,
                reservation_id=reservation.id,
                account_id=spec.account_id,
                event_id=spec.event_id,
                invocation_id=partial.invocation_id if partial is not None else None,
                model=spec.model,
                model_version=partial.model_version if partial is not None else None,
                region=spec.region,
                prompt_version=spec.prompt_version,
                purpose=spec.purpose,
                retry_attempt=spec.retry_attempt,
                evaluation=spec.evaluation,
                outcome="failed",
                prompt_tokens=prompt_tokens,
                response_tokens=response_tokens,
                thinking_tokens=thinking_tokens,
                total_tokens=total_tokens,
                actual_usd_nanos=actual_usd_nanos,
                actual_dkk_micros=usd_nanos_to_conservative_dkk_micros(actual_usd_nanos),
                reserved_dkk_micros=reservation.reserved_dkk_micros,
                error_code=(partial.error_code if partial is not None else type(error).__name__),
            )
        )
        _emit_model_usage(spec, usage)
        raise

    actual_usd_nanos = model_cost_usd_nanos(
        model=spec.model,
        prompt_tokens=completed.prompt_tokens,
        response_tokens=completed.response_tokens,
        thinking_tokens=completed.thinking_tokens,
    )
    usage = await repository.record_model_usage(
        ModelUsageRecord(
            id=reservation.id,
            reservation_id=reservation.id,
            account_id=spec.account_id,
            event_id=spec.event_id,
            invocation_id=completed.invocation_id,
            model=spec.model,
            model_version=completed.model_version,
            region=spec.region,
            prompt_version=spec.prompt_version,
            purpose=spec.purpose,
            retry_attempt=spec.retry_attempt,
            evaluation=spec.evaluation,
            outcome="succeeded",
            prompt_tokens=completed.prompt_tokens,
            response_tokens=completed.response_tokens,
            thinking_tokens=completed.thinking_tokens,
            total_tokens=completed.total_tokens,
            actual_usd_nanos=actual_usd_nanos,
            actual_dkk_micros=usd_nanos_to_conservative_dkk_micros(actual_usd_nanos),
            reserved_dkk_micros=reservation.reserved_dkk_micros,
        )
    )
    _emit_model_usage(spec, usage)
    return AccountedModelInvocation(
        result=completed.result,
        reservation=reservation,
        usage=usage,
    )
