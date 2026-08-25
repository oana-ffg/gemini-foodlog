import asyncio
from base64 import b64decode
from pathlib import Path

from fastapi.testclient import TestClient

from foodlog_backend.app import create_app
from foodlog_backend.errors import AccountCapacityReached
from foodlog_backend.inference import FixtureInferenceEngine, verify_fixture_files
from foodlog_backend.models import MealInference
from foodlog_backend.settings import Settings

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "images"
ADVERSARIAL_FIXTURES = FIXTURES / "adversarial"
USER_HEADER = {"X-FoodLog-Local-User": "owner-a"}


def build_client() -> TestClient:
    return TestClient(create_app(Settings(environment="test")))


def test_concurrent_account_provisioning_is_idempotent() -> None:
    app = create_app(Settings(environment="test"))
    repository = app.state.container.repository

    async def provision_concurrently() -> list:
        return await asyncio.gather(
            *(repository.provision_account("same-owner") for _ in range(50))
        )

    accounts = asyncio.run(provision_concurrently())

    assert len({account.id for account in accounts}) == 1
    assert all(account.owner_user_id == "same-owner" for account in accounts)


def test_concurrent_public_admission_never_exceeds_the_configured_limit() -> None:
    app = create_app(Settings(environment="test", public_account_limit=25))
    repository = app.state.container.repository

    async def provision_competing_users() -> list:
        return await asyncio.gather(
            *(repository.provision_account(f"competing-owner-{index}") for index in range(50)),
            return_exceptions=True,
        )

    results = asyncio.run(provision_competing_users())
    admitted = [result for result in results if not isinstance(result, Exception)]
    rejected = [result for result in results if isinstance(result, Exception)]

    assert len(admitted) == 25
    assert len({account.id for account in admitted}) == 25
    assert len(rejected) == 25
    assert all(isinstance(error, AccountCapacityReached) for error in rejected)


def test_capacity_response_is_stable_and_admitted_user_remains_idempotent() -> None:
    app = create_app(Settings(environment="test", public_account_limit=1))
    with TestClient(app) as client:
        admitted = client.post(
            "/v1/accounts",
            headers={"X-FoodLog-Local-User": "admitted-owner"},
        )
        overflow = client.post(
            "/v1/accounts",
            headers={"X-FoodLog-Local-User": "waitlisted-owner"},
        )
        retry = client.post(
            "/v1/accounts",
            headers={"X-FoodLog-Local-User": "admitted-owner"},
        )

    assert admitted.status_code == retry.status_code == 200
    assert admitted.json()["id"] == retry.json()["id"]
    assert overflow.status_code == 409
    assert overflow.json() == {"detail": "signup_capacity_exhausted"}


def test_preview_requires_both_iam_defense_and_shared_secret() -> None:
    settings = Settings(
        environment="preview",
        preview_shared_secret="preview-secret-that-is-at-least-32-characters",
    )
    with TestClient(create_app(settings)) as client:
        missing_secret = client.post("/v1/accounts", headers=USER_HEADER)
        wrong_secret = client.post(
            "/v1/accounts",
            headers={**USER_HEADER, "X-FoodLog-Preview-Secret": "x" * 32},
        )
        accepted = client.post(
            "/v1/accounts",
            headers={
                **USER_HEADER,
                "X-FoodLog-Preview-Secret": settings.preview_shared_secret,
            },
        )

        assert missing_secret.status_code == 401
        assert wrong_secret.status_code == 401
        assert accepted.status_code == 200


def provision(client: TestClient, user: str = "owner-a") -> tuple[dict, dict]:
    headers = {"X-FoodLog-Local-User": user}
    account = client.post("/v1/accounts", headers=headers)
    assert account.status_code == 200
    camera = client.post(
        "/v1/browser-cameras",
        headers=headers,
        json={"name": "Test browser camera"},
    )
    assert camera.status_code == 200
    return account.json(), camera.json()


def test_fixture_capture_creates_explainable_journal_entry() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
        response = client.post(
            f"/v1/browser-cameras/{camera['id']}/captures",
            headers={**USER_HEADER, "Idempotency-Key": "capture-steak-0001"},
            files={"image": ("capture.png", image, "image/png")},
        )
        assert response.status_code == 202
        assert response.json()["accepted_image_count"] == 1

        journal = client.get("/v1/journal", headers=USER_HEADER)
        assert journal.status_code == 200
        entry = journal.json()[0]
        assert entry["title"] == "Air-fried steak"
        assert entry["confidence"] == "confident"
        assert entry["observations"]

        image_response = client.get(
            f"/v1/captures/{entry['capture_id']}/image",
            headers=USER_HEADER,
        )
        assert image_response.status_code == 200
        assert image_response.content == image
        assert image_response.headers["cache-control"] == "private, no-store"


def test_idempotent_retry_does_not_consume_quota_or_duplicate_meal() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-chicken-airfryer.png").read_bytes()
        request = {
            "headers": {**USER_HEADER, "Idempotency-Key": "capture-chicken-0001"},
            "files": {"image": ("capture.png", image, "image/png")},
        }
        first = client.post(f"/v1/browser-cameras/{camera['id']}/captures", **request)
        retry = client.post(f"/v1/browser-cameras/{camera['id']}/captures", **request)
        assert first.status_code == retry.status_code == 202
        assert retry.json()["duplicate"] is True
        assert retry.json()["accepted_image_count"] == 1
        assert len(client.get("/v1/journal", headers=USER_HEADER).json()) == 1


def test_cross_account_camera_and_capture_access_fail_closed() -> None:
    with build_client() as client:
        _, camera_a = provision(client, "owner-a")
        _, _camera_b = provision(client, "owner-b")
        image = (FIXTURES / "synthetic-leftover-pasta.png").read_bytes()
        rejected = client.post(
            f"/v1/browser-cameras/{camera_a['id']}/captures",
            headers={
                "X-FoodLog-Local-User": "owner-b",
                "Idempotency-Key": "cross-account-0001",
            },
            files={"image": ("capture.png", image, "image/png")},
        )
        assert rejected.status_code == 404

        accepted = client.post(
            f"/v1/browser-cameras/{camera_a['id']}/captures",
            headers={**USER_HEADER, "Idempotency-Key": "owner-capture-0001"},
            files={"image": ("capture.png", image, "image/png")},
        )
        capture_id = accepted.json()["capture_id"]
        hidden = client.get(
            f"/v1/captures/{capture_id}/image",
            headers={"X-FoodLog-Local-User": "owner-b"},
        )
        assert hidden.status_code == 404


def test_unknown_image_is_explicitly_uncertain_without_model_call() -> None:
    with build_client() as client:
        _, camera = provision(client)
        one_pixel_png = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        response = client.post(
            f"/v1/browser-cameras/{camera['id']}/captures",
            headers={**USER_HEADER, "Idempotency-Key": "unknown-capture-0001"},
            files={"image": ("capture.png", one_pixel_png, "image/png")},
        )
        assert response.status_code == 202
        entry = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        assert entry["confidence"] == "uncertain"
        assert "never calls Gemini" in entry["rationale"]


def test_adversarial_camera_fixtures_remain_uncertain_without_model_call() -> None:
    fixture_paths = sorted(ADVERSARIAL_FIXTURES.glob("*.png"))
    assert len(fixture_paths) == 3

    for index, fixture_path in enumerate(fixture_paths):
        with build_client() as client:
            _, camera = provision(client)
            response = client.post(
                f"/v1/browser-cameras/{camera['id']}/captures",
                headers={
                    **USER_HEADER,
                    "Idempotency-Key": f"adversarial-capture-{index:02d}",
                },
                files={
                    "image": (fixture_path.name, fixture_path.read_bytes(), "image/png")
                },
            )

            assert response.status_code == 202
            entry = client.get("/v1/journal", headers=USER_HEADER).json()[0]
            assert entry["title"] == "Unrecognized kitchen activity"
            assert entry["confidence"] == "uncertain"
            questions = client.get("/v1/questions", headers=USER_HEADER).json()
            assert len(questions) == 1
            assert questions[0]["meal_id"] == entry["id"]


def test_declared_image_type_must_match_content() -> None:
    with build_client() as client:
        _, camera = provision(client)
        response = client.post(
            f"/v1/browser-cameras/{camera['id']}/captures",
            headers={**USER_HEADER, "Idempotency-Key": "invalid-image-0001"},
            files={"image": ("capture.png", b"not-a-real-png", "image/png")},
        )
        assert response.status_code == 415


def test_fixture_directory_matches_registered_ground_truth() -> None:
    assert verify_fixture_files(FIXTURES) == []


class FailOnceInferenceEngine:
    def __init__(self) -> None:
        self._failed = False
        self._delegate = FixtureInferenceEngine()

    async def infer(self, image: bytes, content_type: str) -> MealInference:
        if not self._failed:
            self._failed = True
            raise RuntimeError("simulated inference failure")
        return await self._delegate.infer(image, content_type)


def test_failed_processing_rolls_back_image_quota_and_idempotency() -> None:
    app = create_app(
        Settings(environment="test"),
        inference_engine=FailOnceInferenceEngine(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
        request = {
            "headers": {**USER_HEADER, "Idempotency-Key": "retry-after-failure-0001"},
            "files": {"image": ("capture.png", image, "image/png")},
        }

        failed = client.post(f"/v1/browser-cameras/{camera['id']}/captures", **request)
        retried = client.post(f"/v1/browser-cameras/{camera['id']}/captures", **request)

        assert failed.status_code == 500
        assert retried.status_code == 202
        assert retried.json()["accepted_image_count"] == 1
        assert retried.json()["duplicate"] is False
        assert len(client.get("/v1/journal", headers=USER_HEADER).json()) == 1


def test_confirmation_is_idempotent_and_preserves_original_revision() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-steak-airfryer.png").read_bytes()
        capture = client.post(
            f"/v1/browser-cameras/{camera['id']}/captures",
            headers={**USER_HEADER, "Idempotency-Key": "confirm-capture-0001"},
            files={"image": ("capture.png", image, "image/png")},
        )
        meal = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        request = {
            "headers": {**USER_HEADER, "Idempotency-Key": "confirm-feedback-0001"},
            "json": {"kind": "confirm"},
        }

        first = client.post(f"/v1/meals/{meal['id']}/feedback", **request)
        retry = client.post(f"/v1/meals/{meal['id']}/feedback", **request)

        assert capture.status_code == 202
        assert first.status_code == retry.status_code == 200
        assert first.json()["revision"]["id"] == retry.json()["revision"]["id"]
        current = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        assert current["status"] == "confirmed"
        assert current["revision_number"] == 2
        revisions = client.get(
            f"/v1/meals/{meal['id']}/revisions",
            headers=USER_HEADER,
        ).json()
        assert [revision["source"] for revision in revisions] == [
            "inference",
            "user_feedback",
        ]
        assert revisions[0]["inference"]["title"] == "Air-fried steak"


def test_correction_keeps_original_inference_and_supports_unresolved_feedback() -> None:
    with build_client() as client:
        _, camera = provision(client)
        image = (FIXTURES / "synthetic-chicken-airfryer.png").read_bytes()
        client.post(
            f"/v1/browser-cameras/{camera['id']}/captures",
            headers={**USER_HEADER, "Idempotency-Key": "correct-capture-0001"},
            files={"image": ("capture.png", image, "image/png")},
        )
        meal = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        corrected = client.post(
            f"/v1/meals/{meal['id']}/feedback",
            headers={**USER_HEADER, "Idempotency-Key": "correct-feedback-0001"},
            json={
                "kind": "correct",
                "actual_meal": "Turkey breast",
                "explanation": "The fillets came from the turkey package visible earlier.",
            },
        )
        unresolved = client.post(
            f"/v1/meals/{meal['id']}/feedback",
            headers={**USER_HEADER, "Idempotency-Key": "correct-feedback-0002"},
            json={
                "kind": "correct",
                "explanation": "That was not the final meal, but I do not know what replaced it.",
            },
        )

        assert corrected.status_code == unresolved.status_code == 200
        current = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        assert current["status"] == "contradicted"
        assert current["title"] == "Unresolved meal"
        assert current["revision_number"] == 3
        revisions = client.get(
            f"/v1/meals/{meal['id']}/revisions",
            headers=USER_HEADER,
        ).json()
        assert revisions[0]["inference"]["title"] == "Air-fried chicken breast"
        assert revisions[1]["inference"]["title"] == "Turkey breast"
        assert revisions[2]["status"] == "contradicted"


def test_uncertain_question_answer_revises_meal_and_closes_inbox() -> None:
    with build_client() as client:
        _, camera = provision(client)
        one_pixel_png = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        client.post(
            f"/v1/browser-cameras/{camera['id']}/captures",
            headers={**USER_HEADER, "Idempotency-Key": "question-capture-0001"},
            files={"image": ("capture.png", one_pixel_png, "image/png")},
        )
        questions = client.get("/v1/questions", headers=USER_HEADER).json()
        assert len(questions) == 1
        question = questions[0]
        request = {
            "headers": {**USER_HEADER, "Idempotency-Key": "question-answer-0001"},
            "json": {
                "answer": "Vegetable soup",
                "learning_tip": "The blue pot is normally used for soup.",
            },
        }

        first = client.post(f"/v1/questions/{question['id']}/answer", **request)
        retry = client.post(f"/v1/questions/{question['id']}/answer", **request)

        assert first.status_code == retry.status_code == 200
        assert first.json()["revision"]["id"] == retry.json()["revision"]["id"]
        assert client.get("/v1/questions", headers=USER_HEADER).json() == []
        answered = client.get(
            "/v1/questions?question_status=answered",
            headers=USER_HEADER,
        ).json()
        assert answered[0]["answer"] == "Vegetable soup"
        current = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        assert current["title"] == "Vegetable soup"
        assert current["status"] == "corrected"


def test_feedback_and_question_access_are_tenant_scoped() -> None:
    with build_client() as client:
        _, camera = provision(client, "owner-a")
        provision(client, "owner-b")
        one_pixel_png = b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        client.post(
            f"/v1/browser-cameras/{camera['id']}/captures",
            headers={**USER_HEADER, "Idempotency-Key": "scoped-question-capture-0001"},
            files={"image": ("capture.png", one_pixel_png, "image/png")},
        )
        meal = client.get("/v1/journal", headers=USER_HEADER).json()[0]
        question = client.get("/v1/questions", headers=USER_HEADER).json()[0]
        owner_b = {"X-FoodLog-Local-User": "owner-b"}

        feedback = client.post(
            f"/v1/meals/{meal['id']}/feedback",
            headers={**owner_b, "Idempotency-Key": "cross-feedback-0001"},
            json={"kind": "confirm"},
        )
        answer = client.post(
            f"/v1/questions/{question['id']}/answer",
            headers={**owner_b, "Idempotency-Key": "cross-answer-0001"},
            json={"answer": "Not your meal"},
        )

        assert feedback.status_code == 404
        assert answer.status_code == 404
