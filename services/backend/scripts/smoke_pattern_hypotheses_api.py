from __future__ import annotations

import argparse
import asyncio
import os
from uuid import uuid4

import httpx

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    KnowledgeClaim,
    PatternEvidenceExample,
    QuestionEvidenceReference,
)

SYNTHETIC_MARKER = "Synthetic pattern persistence smoke"


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected_status: int,
    headers: dict[str, str] | None = None,
    json: dict[str, object] | None = None,
) -> object:
    response = client.request(method, path, headers=headers, json=json)
    assert response.status_code == expected_status, (
        f"{method} {path}: expected {expected_status}, got {response.status_code}: "
        f"{response.text}"
    )
    return response.json()


async def retained_meal_examples(
    *,
    project_id: str,
    owner_user_id: str,
) -> list[PatternEvidenceExample]:
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    examples: list[PatternEvidenceExample] = []
    for meal in await repository.list_meals(owner_user_id):
        for revision in await repository.list_meal_revisions(owner_user_id, meal.id):
            examples.append(
                PatternEvidenceExample(
                    evidence=QuestionEvidenceReference(
                        kind="meal_revision",
                        id=revision.id,
                    ),
                    occurred_at=meal.occurred_at or meal.created_at,
                    summary=(
                        f"Synthetic persistence smoke cites retained meal revision "
                        f"{revision.number} from event {meal.event_id}."
                    ),
                )
            )
    unique = {example.evidence.id: example for example in examples}
    selected = sorted(unique.values(), key=lambda item: item.occurred_at)
    assert len(selected) >= 4, "production test account needs four retained meal revisions"
    repository._client.close()
    return selected


async def open_pattern(
    *,
    project_id: str,
    account_id: str,
    claim: KnowledgeClaim,
    statement: str,
    supports: list[PatternEvidenceExample],
    ended_at,
):
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    try:
        return await repository.open_pattern_question(
            account_id=account_id,
            prompt=f"{statement}. Is this synthetic test record accurate?",
            reason="Synthetic production persistence smoke using real retained revision IDs.",
            tentative_claim=statement,
            evidence=[item.evidence for item in supports],
            pattern_claim=claim,
            observation_started_at=min(item.occurred_at for item in supports),
            observation_ended_at=ended_at,
            supporting_examples=supports,
            counterexamples=[],
            prompt_version="pattern-persistence-smoke-v1",
        )
    finally:
        repository._client.close()


def cleanup_prior_smoke_records(client: httpx.Client) -> None:
    open_questions = request_json(client, "GET", "/v1/questions", expected_status=200)
    assert isinstance(open_questions, list)
    for question in open_questions:
        if not (question.get("tentative_claim") or "").startswith(SYNTHETIC_MARKER):
            continue
        request_json(
            client,
            "POST",
            f"/v1/questions/{question['id']}/responses",
            expected_status=200,
            headers={"Idempotency-Key": f"pattern-smoke-recover-{question['id']}"},
            json={"kind": "reject", "explanation": "Recovered synthetic smoke cleanup."},
        )

    active_pages = request_json(client, "GET", "/v1/knowledge", expected_status=200)
    assert isinstance(active_pages, list)
    for page in active_pages:
        if not page["statement"].startswith(SYNTHETIC_MARKER):
            continue
        request_json(
            client,
            "POST",
            f"/v1/knowledge/{page['id']}/retire",
            expected_status=200,
            headers={"Idempotency-Key": f"pattern-smoke-retire-{page['id']}"},
            json={
                "expected_revision_number": page["current_revision_number"],
                "reason": "Recovered synthetic pattern smoke cleanup.",
            },
        )


def smoke(args: argparse.Namespace) -> None:
    email = os.environ["FOODLOG_SMOKE_EMAIL"]
    password = os.environ["FOODLOG_SMOKE_PASSWORD"]
    firebase_response = httpx.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
        params={"key": args.firebase_api_key},
        headers={"Origin": args.origin, "Referer": f"{args.origin}/"},
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=30,
    )
    firebase_response.raise_for_status()
    firebase_payload = firebase_response.json()
    token = firebase_payload["idToken"]
    owner_user_id = firebase_payload["localId"]
    run_id = uuid4().hex[:12]

    with httpx.Client(
        base_url=args.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        timeout=30,
    ) as client:
        account = request_json(client, "POST", "/v1/accounts", expected_status=200)
        assert isinstance(account, dict)
        account_id = account["id"]
        cleanup_prior_smoke_records(client)

        examples = asyncio.run(
            retained_meal_examples(
                project_id=args.project,
                owner_user_id=owner_user_id,
            )
        )
        common_condition = f"synthetic-smoke-{run_id}"

        confirmed_question = asyncio.run(
            open_pattern(
                project_id=args.project,
                account_id=account_id,
                claim=KnowledgeClaim(
                    dimension="synthetic smoke pattern",
                    value="confirmed",
                    conditions=(common_condition, "confirmation path"),
                ),
                statement=f"{SYNTHETIC_MARKER} {run_id}: confirmation path",
                supports=examples[:2],
                ended_at=examples[1].occurred_at,
            )
        )
        confirm_request = {
            "headers": {"Idempotency-Key": f"pattern-smoke-confirm-{run_id}"},
            "json": {"kind": "confirm", "explanation": "Synthetic contract confirmation."},
        }
        confirmed = request_json(
            client,
            "POST",
            f"/v1/questions/{confirmed_question.id}/responses",
            expected_status=200,
            **confirm_request,
        )
        confirmed_retry = request_json(
            client,
            "POST",
            f"/v1/questions/{confirmed_question.id}/responses",
            expected_status=200,
            **confirm_request,
        )
        assert confirmed_retry == confirmed
        assert isinstance(confirmed, dict)
        assert confirmed["knowledge"]["revision"]["claim"] is not None
        confirmed_page = confirmed["knowledge"]["page"]

        corrected_question = asyncio.run(
            open_pattern(
                project_id=args.project,
                account_id=account_id,
                claim=KnowledgeClaim(
                    dimension="synthetic smoke pattern",
                    value="original",
                    conditions=(common_condition, "correction path"),
                ),
                statement=f"{SYNTHETIC_MARKER} {run_id}: correction path",
                supports=examples[:2],
                ended_at=examples[1].occurred_at,
            )
        )
        correction = f"{SYNTHETIC_MARKER} {run_id}: exact corrected wording"
        corrected = request_json(
            client,
            "POST",
            f"/v1/questions/{corrected_question.id}/responses",
            expected_status=200,
            headers={"Idempotency-Key": f"pattern-smoke-correct-{run_id}"},
            json={"kind": "correct", "correction": correction},
        )
        assert isinstance(corrected, dict)
        assert corrected["knowledge"]["revision"]["statement"] == correction
        assert corrected["knowledge"]["revision"]["claim"] is None
        corrected_page = corrected["knowledge"]["page"]

        rejected_question = asyncio.run(
            open_pattern(
                project_id=args.project,
                account_id=account_id,
                claim=KnowledgeClaim(
                    dimension="synthetic smoke pattern",
                    value="rejected",
                    conditions=(common_condition, "resurface path"),
                ),
                statement=f"{SYNTHETIC_MARKER} {run_id}: resurface path",
                supports=examples[:2],
                ended_at=examples[1].occurred_at,
            )
        )
        rejected = request_json(
            client,
            "POST",
            f"/v1/questions/{rejected_question.id}/responses",
            expected_status=200,
            headers={"Idempotency-Key": f"pattern-smoke-reject-{run_id}"},
            json={"kind": "reject", "explanation": "Synthetic rejection path."},
        )
        assert isinstance(rejected, dict) and rejected["knowledge"] is None
        one_new = asyncio.run(
            open_pattern(
                project_id=args.project,
                account_id=account_id,
                claim=rejected_question.pattern_claim,
                statement=rejected_question.tentative_claim,
                supports=examples[:3],
                ended_at=examples[2].occurred_at,
            )
        )
        assert one_new.id == rejected_question.id
        resurfaced = asyncio.run(
            open_pattern(
                project_id=args.project,
                account_id=account_id,
                claim=rejected_question.pattern_claim,
                statement=rejected_question.tentative_claim,
                supports=examples[:4],
                ended_at=examples[3].occurred_at,
            )
        )
        assert resurfaced.id != rejected_question.id
        assert resurfaced.predecessor_question_id == rejected_question.id
        request_json(
            client,
            "POST",
            f"/v1/questions/{resurfaced.id}/responses",
            expected_status=200,
            headers={"Idempotency-Key": f"pattern-smoke-resurface-reject-{run_id}"},
            json={"kind": "reject", "explanation": "Synthetic resurface cleanup."},
        )

        for page in (confirmed_page, corrected_page):
            retired = request_json(
                client,
                "POST",
                f"/v1/knowledge/{page['id']}/retire",
                expected_status=200,
                headers={"Idempotency-Key": f"pattern-smoke-retire-{page['id']}"},
                json={
                    "expected_revision_number": page["current_revision_number"],
                    "reason": "Synthetic pattern persistence smoke cleanup.",
                },
            )
            assert isinstance(retired, dict)
            assert retired["revision"]["lifecycle"] == "retired"

    print(f"account_id={account_id}")
    print(f"confirmed_question_id={confirmed_question.id}")
    print(f"corrected_question_id={corrected_question.id}")
    print(f"rejected_question_id={rejected_question.id}")
    print(f"resurfaced_question_id={resurfaced.id}")
    print("exact_response_retry=true")
    print("confirm_and_correction_knowledge=true")
    print("rejection_without_knowledge=true")
    print("resurface_threshold=2")
    print("synthetic_knowledge_retired=true")
    print("model_calls=0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--project", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
