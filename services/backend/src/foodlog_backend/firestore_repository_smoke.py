from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.async_client import AsyncClient

from .firestore_repository import FirestoreRepository
from .models import (
    Confidence,
    MealComponent,
    MealEntry,
    MealFeedbackKind,
    MealFeedbackRequest,
    QuestionAnswerRequest,
    QuestionStatus,
    utc_now,
)
from .repository import Repository

SMOKE_ACCOUNT_ID = "repository-smoke-account-v1"
SMOKE_OWNER_ID = "repository-smoke-owner-v1"
SMOKE_CAPTURE_ID = "repository-smoke-capture-v1"
SMOKE_MEAL_ID = "repository-smoke-meal-v1"
SMOKE_FIXTURE_VERSION = "firestore-repository-smoke-v1"


async def _ensure_document(reference, expected: dict[str, Any]) -> None:
    snapshot = await reference.get()
    if not snapshot.exists:
        try:
            await reference.create(expected)
            return
        except AlreadyExists:
            snapshot = await reference.get()
    data = snapshot.to_dict() or {}
    for field, value in expected.items():
        if field == "created_at":
            continue
        if data.get(field) != value:
            raise RuntimeError(f"Smoke fixture collision at {reference.path}: {field}")


async def ensure_smoke_fixture(client: AsyncClient) -> None:
    created_at = utc_now()
    account_ref = client.collection("accounts").document(SMOKE_ACCOUNT_ID)
    await _ensure_document(
        account_ref,
        {
            "schema_version": 1,
            "id": SMOKE_ACCOUNT_ID,
            "owner_user_id": SMOKE_OWNER_ID,
            "entitlement_mode": "unlimited",
            "status": "active",
            "smoke_fixture": SMOKE_FIXTURE_VERSION,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    await _ensure_document(
        account_ref.collection("entitlements").document("current"),
        {
            "schema_version": 1,
            "accepted_image_count": 0,
            "entitlement_mode": "unlimited",
            "trial_image_limit": None,
            "smoke_fixture": SMOKE_FIXTURE_VERSION,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    await _ensure_document(
        client.collection("identities").document(SMOKE_OWNER_ID),
        {
            "schema_version": 1,
            "account_id": SMOKE_ACCOUNT_ID,
            "account_class": "internal_smoke",
            "status": "active",
            "smoke_fixture": SMOKE_FIXTURE_VERSION,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    await _ensure_document(
        account_ref.collection("captures").document(SMOKE_CAPTURE_ID),
        {
            "schema_version": 1,
            "id": SMOKE_CAPTURE_ID,
            "account_id": SMOKE_ACCOUNT_ID,
            "status": "stored",
            "smoke_fixture": SMOKE_FIXTURE_VERSION,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )


async def run_smoke(repository: Repository) -> dict[str, Any]:
    initial_meal = MealEntry(
        id=SMOKE_MEAL_ID,
        account_id=SMOKE_ACCOUNT_ID,
        capture_id=SMOKE_CAPTURE_ID,
        title="Likely air-fried steak",
        confidence=Confidence.UNCERTAIN,
        components=[
            MealComponent(
                name="Red meat",
                ingredients=["red meat"],
                preparation_methods=["air frying"],
            )
        ],
        observations=["Red meat is visible beside an air-fryer basket."],
        alternatives=["Air-fried lamb"],
        rationale="The meat appears red, but the distant view does not show the cut.",
        clarification_question="Was this steak or lamb?",
        clarification_reason="The answer distinguishes the supported red-meat options.",
    )
    meal = await repository.save_meal(account_id=SMOKE_ACCOUNT_ID, meal=initial_meal)
    question = await repository.open_question(
        account_id=SMOKE_ACCOUNT_ID,
        meal=meal,
        prompt="Was this steak or lamb?",
        reason="The distant view supports both options.",
    )
    confirmation = await repository.record_meal_feedback(
        owner_user_id=SMOKE_OWNER_ID,
        meal_id=meal.id,
        request=MealFeedbackRequest(kind=MealFeedbackKind.CONFIRM),
        idempotency_key="repository-smoke-confirm-v1",
    )
    answer = await repository.answer_question(
        owner_user_id=SMOKE_OWNER_ID,
        question_id=question.id,
        request=QuestionAnswerRequest(
            answer="Steak",
            learning_tip="The beef packet has a dark green label.",
        ),
        idempotency_key="repository-smoke-answer-v1",
    )
    current = await repository.meal_for_owner(SMOKE_OWNER_ID, meal.id)
    revisions = await repository.list_meal_revisions(SMOKE_OWNER_ID, meal.id)
    questions = await repository.list_questions(
        SMOKE_OWNER_ID,
        question_status=QuestionStatus.ANSWERED,
    )
    if confirmation.revision.number != 2:
        raise RuntimeError("Confirmation did not persist immutable revision 2")
    if answer.revision.number != 3 or current.revision_number != 3:
        raise RuntimeError("Question answer did not atomically persist revision 3")
    if [revision.number for revision in revisions] != [1, 2, 3]:
        raise RuntimeError("Repository smoke revision history is incomplete")
    if len(questions) != 1 or questions[0].id != question.id:
        raise RuntimeError("Repository smoke question did not close exactly once")
    return {
        "schema_version": SMOKE_FIXTURE_VERSION,
        "account_id": SMOKE_ACCOUNT_ID,
        "capture_id": SMOKE_CAPTURE_ID,
        "meal_id": SMOKE_MEAL_ID,
        "meal_revision": current.revision_number,
        "revision_numbers": [revision.number for revision in revisions],
        "question_status": questions[0].status,
        "feedback_revision_numbers": [
            confirmation.revision.number,
            answer.revision.number,
        ],
        "model_calls": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify production Firestore meal, question, feedback, and revision writes."
    )
    parser.add_argument(
        "--confirm-isolated-smoke",
        action="store_true",
        help="Confirm this execution may create or reuse the isolated smoke fixture.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_isolated_smoke:
        _parser().error("--confirm-isolated-smoke is required")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required")
    client = AsyncClient(project=project_id)
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
        client=client,
    )

    async def execute() -> dict[str, Any]:
        await ensure_smoke_fixture(client)
        return await run_smoke(repository)

    try:
        print(json.dumps(asyncio.run(execute()), indent=2, sort_keys=True))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
