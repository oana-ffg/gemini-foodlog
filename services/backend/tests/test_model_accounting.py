from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest
from google.cloud.firestore_v1.async_client import AsyncClient

from foodlog_backend.errors import (
    ModelInvocationAlreadyReconciled,
    ModelSpendLimitExceeded,
)
from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.model_accounting import (
    CompletedModelInvocation,
    ModelInvocationExecutionError,
    ModelInvocationSpec,
    conservative_reservation_dkk_micros,
    execute_accounted_model_invocation,
    model_cost_usd_nanos,
    reservation_id_for_invocation,
)
from foodlog_backend.repository import InMemoryRepository


def invocation_spec(invocation_key: str = "accounted-call-0001") -> ModelInvocationSpec:
    return ModelInvocationSpec(
        invocation_key=invocation_key,
        account_id="replaced-after-provision",
        event_id="accounted-event-0001",
        model="gemini-3.6-flash",
        region="eu",
        purpose="event_inference",
        prompt_version="food-event-v4",
        max_prompt_tokens=1_000,
        max_output_tokens=100,
        max_billable_calls=1,
        retry_attempt=0,
        evaluation=False,
    )


def test_integer_model_cost_and_reservation_are_conservative() -> None:
    spec = invocation_spec()

    assert model_cost_usd_nanos(
        model=spec.model,
        prompt_tokens=100,
        response_tokens=10,
        thinking_tokens=5,
    ) == 144_375
    assert conservative_reservation_dkk_micros(spec) == 9_900


def test_accounted_invocation_reserves_before_call_and_reconciles_once() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(
            public_account_limit=25,
            trial_image_limit=200,
            model_spend_limit_dkk_micros=20_000,
        )
        account = await repository.provision_account("accounted-owner")
        spec = replace(invocation_spec(), account_id=account.id)
        calls = 0

        async def invoke() -> CompletedModelInvocation[str]:
            nonlocal calls
            calls += 1
            assert reservation_id_for_invocation(spec) in repository._model_spend_reservations
            return CompletedModelInvocation(
                result="model-result",
                invocation_id="provider-invocation-0001",
                model_version="gemini-3.6-flash-001",
                prompt_tokens=100,
                response_tokens=10,
                thinking_tokens=5,
                total_tokens=115,
            )

        accounted = await execute_accounted_model_invocation(
            repository=repository,
            spec=spec,
            invoke=invoke,
        )

        assert accounted.result == "model-result"
        assert accounted.usage.actual_usd_nanos == 144_375
        assert accounted.usage.actual_dkk_micros == 1_155
        assert accounted.usage.reserved_dkk_micros == 9_900
        assert repository._model_spend_reserved_dkk_micros == 9_900
        assert repository._model_spend_actual_dkk_micros == 1_155
        assert calls == 1

        with pytest.raises(ModelInvocationAlreadyReconciled):
            await execute_accounted_model_invocation(
                repository=repository,
                spec=spec,
                invoke=invoke,
            )
        assert calls == 1
        assert len(repository._model_usage) == 1

    asyncio.run(scenario())


def test_limit_rejection_and_failed_call_never_hide_or_repeat_spend_state() -> None:
    async def scenario() -> None:
        account_repository = InMemoryRepository(
            public_account_limit=25,
            trial_image_limit=200,
            model_spend_limit_dkk_micros=9_899,
        )
        account = await account_repository.provision_account("limit-owner")
        spec = replace(invocation_spec(), account_id=account.id)
        called = False

        async def should_not_run() -> CompletedModelInvocation[str]:
            nonlocal called
            called = True
            raise AssertionError("model call ran after the hard ceiling")

        with pytest.raises(ModelSpendLimitExceeded):
            await execute_accounted_model_invocation(
                repository=account_repository,
                spec=spec,
                invoke=should_not_run,
            )
        assert called is False
        assert account_repository._model_usage == {}

        failure_repository = InMemoryRepository(
            public_account_limit=25,
            trial_image_limit=200,
            model_spend_limit_dkk_micros=20_000,
        )
        failure_account = await failure_repository.provision_account("failure-owner")
        failed_spec = replace(
            invocation_spec("accounted-call-failure"),
            account_id=failure_account.id,
            retry_attempt=1,
        )

        async def fail() -> CompletedModelInvocation[str]:
            raise ModelInvocationExecutionError(
                error_code="TimeoutError",
                invocation_id="failed-provider-invocation",
                model_version="gemini-3.6-flash-001",
                prompt_tokens=100,
                response_tokens=10,
                thinking_tokens=5,
                total_tokens=115,
            )

        with pytest.raises(ModelInvocationExecutionError):
            await execute_accounted_model_invocation(
                repository=failure_repository,
                spec=failed_spec,
                invoke=fail,
            )
        usage = await failure_repository.model_usage_for_reservation(
            account_id=failure_account.id,
            reservation_id=reservation_id_for_invocation(failed_spec),
        )
        assert usage is not None
        assert usage.outcome == "failed"
        assert usage.error_code == "TimeoutError"
        assert usage.actual_dkk_micros == 1_155
        assert usage.prompt_tokens == 100
        assert usage.retry_attempt == 1

    asyncio.run(scenario())


@pytest.mark.skipif(
    "FIRESTORE_EMULATOR_HOST" not in os.environ,
    reason="requires the Firestore emulator",
)
def test_firestore_reservation_and_usage_reconcile_atomically() -> None:
    async def scenario() -> None:
        project_id = "gemini-foodlog-model-accounting-test"
        client = AsyncClient(project=project_id)
        account_id = "model-accounting-account"
        await client.collection("accounts").document(account_id).set(
            {
                "schema_version": 1,
                "id": account_id,
                "owner_user_id": "model-accounting-owner",
                "status": "active",
            }
        )
        repository = FirestoreRepository(
            project_id=project_id,
            public_account_limit=25,
            trial_image_limit=200,
            model_spend_limit_dkk_micros=20_000,
            model_spend_ledger_id="model_accounting_test",
            client=client,
        )
        spec = replace(invocation_spec(), account_id=account_id, evaluation=True)
        calls = 0

        async def invoke() -> CompletedModelInvocation[str]:
            nonlocal calls
            calls += 1
            return CompletedModelInvocation(
                result="firestore-model-result",
                invocation_id="firestore-provider-invocation",
                model_version="gemini-3.6-flash-001",
                prompt_tokens=100,
                response_tokens=10,
                thinking_tokens=5,
                total_tokens=115,
            )

        accounted = await execute_accounted_model_invocation(
            repository=repository,
            spec=spec,
            invoke=invoke,
        )
        with pytest.raises(ModelInvocationAlreadyReconciled):
            await execute_accounted_model_invocation(
                repository=repository,
                spec=spec,
                invoke=invoke,
            )

        ledger = await client.collection("system").document("model_accounting_test").get()
        usage = await (
            client.collection("accounts")
            .document(account_id)
            .collection("model_usage")
            .document(accounted.reservation.id)
            .get()
        )
        assert calls == 1
        assert ledger.get("reserved_dkk_micros") == 9_900
        assert ledger.get("actual_dkk_micros") == 1_155
        assert ledger.get("reconciled_reservation_count") == 1
        assert usage.get("outcome") == "succeeded"
        assert usage.get("evaluation") is True
        assert usage.get("retry_attempt") == 0
        assert usage.get("prompt_tokens") == 100
        client.close()

    asyncio.run(scenario())
