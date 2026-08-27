from __future__ import annotations

import argparse
import os

import httpx


def _assert_pattern(question: dict[str, object], account_id: str) -> None:
    assert question["account_id"] == account_id
    assert question["kind"] == "pattern_hypothesis"
    assert question["tentative_claim"]
    assert question["pattern_claim"]
    assert question["pattern_observation_started_at"]
    assert question["pattern_observation_ended_at"]
    assert question["pattern_supporting_examples"]
    assert question["pattern_prompt_version"]
    assert "pattern_uncertainty" in question


def smoke(args: argparse.Namespace) -> None:
    firebase_response = httpx.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
        params={"key": args.firebase_api_key},
        headers={"Origin": args.origin, "Referer": f"{args.origin}/"},
        json={
            "email": os.environ["FOODLOG_SMOKE_EMAIL"],
            "password": os.environ["FOODLOG_SMOKE_PASSWORD"],
            "returnSecureToken": True,
        },
        timeout=30,
    )
    firebase_response.raise_for_status()
    token = firebase_response.json()["idToken"]

    unauthenticated = httpx.get(
        f"{args.api_url}/v1/questions?kind=pattern_hypothesis",
        timeout=30,
    )
    assert unauthenticated.status_code == 401, unauthenticated.text

    with httpx.Client(
        base_url=args.api_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": args.origin,
            "Referer": f"{args.origin}/",
        },
        timeout=30,
    ) as client:
        account_response = client.post("/v1/accounts")
        assert account_response.status_code == 200, account_response.text
        account_id = account_response.json()["id"]

        open_response = client.get("/v1/questions?kind=pattern_hypothesis")
        answered_response = client.get(
            "/v1/questions?kind=pattern_hypothesis&question_status=answered"
        )
        event_response = client.get("/v1/questions?kind=event_clarification")
        invalid_response = client.get("/v1/questions?kind=not_a_question_kind")

        for response in (open_response, answered_response, event_response):
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
        assert invalid_response.status_code == 422, invalid_response.text

        open_patterns = open_response.json()
        answered_patterns = answered_response.json()
        event_questions = event_response.json()

        assert all(question["status"] == "open" for question in open_patterns)
        assert all(
            question["status"] == "answered" for question in answered_patterns
        )
        assert all(
            question["kind"] == "event_clarification"
            and question["account_id"] == account_id
            for question in event_questions
        )
        for question in open_patterns + answered_patterns:
            _assert_pattern(question, account_id)
        assert answered_patterns, "test account has no answered pattern evidence"
        assert all(question["response_kind"] for question in answered_patterns)

    print(f"open_pattern_questions={len(open_patterns)}")
    print(f"answered_pattern_questions={len(answered_patterns)}")
    print(f"open_event_questions={len(event_questions)}")
    print("rich_pattern_evidence=verified")
    print("question_kind_separation=verified")
    print("tenant_scope=verified")
    print("private_no_store=true")
    print("unauthenticated_status=401")
    print("invalid_kind_status=422")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    smoke(parse_args())
