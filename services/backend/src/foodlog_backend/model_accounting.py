from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from .errors import ModelInvocationAlreadyReconciled
from .models import ModelSpendReservation, ModelUsageRecord

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
    max_provider_attempts: int
    retry_attempt: int
    evaluation: bool

    def __post_init__(self) -> None:
        for field_name in ("invocation_key", "account_id", "event_id", "region", "purpose"):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} is required")
        if self.model not in MODEL_INPUT_USD_NANOS_PER_TOKEN:
            raise ValueError(f"No reviewed pricing is configured for model {self.model}")
        if min(
            self.max_prompt_tokens,
            self.max_output_tokens,
            self.max_provider_attempts,
        ) <= 0:
            raise ValueError("model invocation ceilings and provider attempts must be positive")
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
    return usd_nanos_to_conservative_dkk_micros(
        per_attempt_usd_nanos * spec.max_provider_attempts
    )


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
        max_provider_attempts=spec.max_provider_attempts,
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
        await repository.record_model_usage(
            ModelUsageRecord(
                id=reservation.id,
                reservation_id=reservation.id,
                account_id=spec.account_id,
                event_id=spec.event_id,
                model=spec.model,
                region=spec.region,
                prompt_version=spec.prompt_version,
                purpose=spec.purpose,
                retry_attempt=spec.retry_attempt,
                evaluation=spec.evaluation,
                outcome="failed",
                prompt_tokens=0,
                response_tokens=0,
                thinking_tokens=0,
                total_tokens=0,
                actual_usd_nanos=0,
                actual_dkk_micros=0,
                reserved_dkk_micros=reservation.reserved_dkk_micros,
                error_code=type(error).__name__,
            )
        )
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
    return AccountedModelInvocation(
        result=completed.result,
        reservation=reservation,
        usage=usage,
    )
