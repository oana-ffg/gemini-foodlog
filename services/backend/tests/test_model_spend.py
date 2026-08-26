import asyncio

import pytest

from foodlog_backend.errors import (
    AccountNotProvisioned,
    ModelSpendLimitExceeded,
    ModelSpendReservationConflict,
)
from foodlog_backend.model_spend_smoke import SMOKE_LIMIT_DKK_MICROS, run_smoke
from foodlog_backend.models import ModelSpendReservation
from foodlog_backend.repository import InMemoryRepository


def _reservation(
    identifier: str,
    *,
    account_id: str,
    amount: int,
    event_id: str = "model-spend-event-0001",
) -> ModelSpendReservation:
    return ModelSpendReservation(
        id=identifier,
        account_id=account_id,
        event_id=event_id,
        reserved_dkk_micros=amount,
    )


def test_model_spend_reservation_is_idempotent_and_stops_at_exact_limit() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(
            public_account_limit=25,
            trial_image_limit=200,
            model_spend_limit_dkk_micros=100,
        )
        account = await repository.provision_account("model-spend-owner")
        first = _reservation("model-spend-reservation-0001", account_id=account.id, amount=60)
        second = _reservation("model-spend-reservation-0002", account_id=account.id, amount=40)

        stored = await repository.reserve_model_spend(first)
        duplicate = await repository.reserve_model_spend(first.model_copy())
        boundary = await repository.reserve_model_spend(second)

        assert stored == first
        assert duplicate == stored
        assert boundary == second
        assert repository._model_spend_reserved_dkk_micros == 100
        assert len(repository._model_spend_reservations) == 2

        with pytest.raises(ModelSpendLimitExceeded):
            await repository.reserve_model_spend(
                _reservation("model-spend-reservation-0003", account_id=account.id, amount=1)
            )
        assert repository._model_spend_reserved_dkk_micros == 100

    asyncio.run(scenario())


def test_model_spend_reservation_rejects_identity_reuse_and_unknown_accounts() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(
            public_account_limit=25,
            trial_image_limit=200,
            model_spend_limit_dkk_micros=100,
        )
        account = await repository.provision_account("model-spend-owner")
        reservation = _reservation(
            "model-spend-reservation-0001",
            account_id=account.id,
            amount=25,
        )
        await repository.reserve_model_spend(reservation)

        with pytest.raises(ModelSpendReservationConflict):
            await repository.reserve_model_spend(
                reservation.model_copy(update={"reserved_dkk_micros": 26})
            )
        with pytest.raises(AccountNotProvisioned):
            await repository.reserve_model_spend(
                _reservation(
                    "model-spend-reservation-unknown",
                    account_id="unknown-account",
                    amount=1,
                )
            )

    asyncio.run(scenario())


def test_concurrent_model_spend_reservations_cannot_cross_the_limit() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(
            public_account_limit=25,
            trial_image_limit=200,
            model_spend_limit_dkk_micros=100,
        )
        account = await repository.provision_account("model-spend-concurrency-owner")
        results = await asyncio.gather(
            *(
                repository.reserve_model_spend(
                    _reservation(
                        f"model-spend-concurrent-{index:04d}",
                        account_id=account.id,
                        amount=30,
                    )
                )
                for index in range(10)
            ),
            return_exceptions=True,
        )

        accepted = [result for result in results if isinstance(result, ModelSpendReservation)]
        rejected = [result for result in results if isinstance(result, ModelSpendLimitExceeded)]
        assert len(accepted) == 3
        assert len(rejected) == 7
        assert repository._model_spend_reserved_dkk_micros == 90

    asyncio.run(scenario())


def test_no_model_smoke_reserves_the_limit_then_rejects_one_more_micro_dkk() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(
            public_account_limit=25,
            trial_image_limit=200,
            model_spend_limit_dkk_micros=SMOKE_LIMIT_DKK_MICROS,
        )
        account = await repository.provision_account("model-spend-smoke-owner")

        result = await run_smoke(
            repository=repository,
            account_id=account.id,
            event_id="model-spend-smoke-event",
        )

        assert result == {
            "schema_version": "model-spend-smoke-v1",
            "ledger_id": "model_spend_smoke",
            "limit_dkk_micros": SMOKE_LIMIT_DKK_MICROS,
            "reserved_dkk_micros": SMOKE_LIMIT_DKK_MICROS,
            "overflow_dkk_micros": 1,
            "rejected_before_model_call": True,
            "model_calls": 0,
        }

    asyncio.run(scenario())
