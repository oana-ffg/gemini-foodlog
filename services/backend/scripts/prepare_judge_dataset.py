from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import firebase_admin
import httpx
from firebase_admin import auth
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.grouping import CaptureGroupingService, GroupingPolicy
from foodlog_backend.models import BrowserCamera, EntitlementMode, NotificationOutboxStatus
from foodlog_backend.notifications import (
    AccountProvisioningService,
    PubSubNotificationPublisher,
)
from foodlog_backend.pattern_detection import PatternDetectionService
from foodlog_backend.storage import GCSObjectStore
from scripts.production_smoke_support import request_json, trace_ids, wait_for_activity
from scripts.synthetic_dataset_support import seed_synthetic_meal

DATASET_CLIENT_VERSION = "judge-demo-v1"
PATTERN_CLIENT_VERSION = "judge-demo-synthetic-pattern-v1"
MAX_MODEL_TRACES = 6


class ContextNoteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4_000)
    valid_from: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def window_is_ordered(self) -> ContextNoteSpec:
        if self.valid_until <= self.valid_from:
            raise ValueError("context note validity window is not ordered")
        if "synthetic" not in self.text.casefold():
            raise ValueError("judge context note must identify itself as synthetic")
        return self


class FeedbackSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_meal: str = Field(min_length=1, max_length=240)
    explanation: str = Field(min_length=1, max_length=2_000)
    learning_disposition: Literal["reusable"]

    @field_validator("explanation")
    @classmethod
    def learning_is_labelled_synthetic(cls, value: str) -> str:
        if "synthetic" not in value.casefold():
            raise ValueError("judge learning must identify itself as synthetic")
        return value


class RealScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9-]+$")
    attempt: int = Field(default=1, ge=1, le=3)
    fixture: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime
    expected_kind: Literal["tentative_meal", "likely_non_cooking"]
    expected_confidence: Literal["uncertain"] | None = None
    feedback: FeedbackSpec | None = None
    must_cite_previous_learning: bool = False
    discard_explanation: str | None = Field(default=None, min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def action_matches_scenario(self) -> RealScenarioSpec:
        if self.feedback is not None and self.expected_kind != "tentative_meal":
            raise ValueError("only a tentative meal can receive learning feedback")
        if self.discard_explanation is not None and self.expected_kind != "likely_non_cooking":
            raise ValueError("only a likely non-cooking scenario can be discarded")
        return self


class PatternEventSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captured_at: datetime
    title: str = Field(min_length=1, max_length=240)


class PatternHistorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    events: list[PatternEventSpec] = Field(min_length=3, max_length=20)
    provenance_label: str = Field(min_length=1, max_length=500)
    expected_claim_value: str = Field(min_length=1, max_length=200)
    expected_condition: str = Field(min_length=1, max_length=80)

    @field_validator("provenance_label")
    @classmethod
    def provenance_is_labelled_synthetic(cls, value: str) -> str:
        if "synthetic" not in value.casefold() or "no-model" not in value.casefold():
            raise ValueError("pattern provenance must say synthetic and no-model")
        return value


class JudgeDatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    dataset_id: str = Field(pattern=r"^[a-z0-9-]+$")
    context_note: ContextNoteSpec
    real_inference_scenarios: list[RealScenarioSpec] = Field(min_length=1, max_length=6)
    synthetic_pattern_history: PatternHistorySpec

    @model_validator(mode="after")
    def scenario_keys_are_unique(self) -> JudgeDatasetSpec:
        keys = [scenario.key for scenario in self.real_inference_scenarios]
        if len(keys) != len(set(keys)):
            raise ValueError("judge scenario keys must be unique")
        feedback_count = sum(item.feedback is not None for item in self.real_inference_scenarios)
        if feedback_count != 1:
            raise ValueError("judge dataset requires exactly one learning correction")
        if sum(item.must_cite_previous_learning for item in self.real_inference_scenarios) != 1:
            raise ValueError("judge dataset requires exactly one learned follow-up")
        return self


def checked_fixture(root: Path, relative_path: Path, expected_sha256: str) -> Path:
    path = (root / relative_path).resolve()
    if root.resolve() not in path.parents:
        raise ValueError("fixture path escapes the fixture root")
    content = path.read_bytes()
    if sha256(content).hexdigest() != expected_sha256:
        raise ValueError(f"fixture hash mismatch for {relative_path.as_posix()}")
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"fixture is not a PNG: {relative_path.as_posix()}")
    return path


def load_dataset(path: Path, fixture_root: Path) -> JudgeDatasetSpec:
    spec = JudgeDatasetSpec.model_validate_json(path.read_text())
    for scenario in spec.real_inference_scenarios:
        checked_fixture(fixture_root, scenario.fixture, scenario.sha256)
    pattern = spec.synthetic_pattern_history
    checked_fixture(fixture_root, pattern.fixture, pattern.sha256)
    return spec


def firebase_identity(
    *,
    project_id: str,
    email: str,
    password: str,
    allow_create: bool,
) -> auth.UserRecord:
    app = firebase_admin.initialize_app(
        options={"projectId": project_id},
        name=f"judge-dataset-{os.getpid()}",
    )
    try:
        try:
            user = auth.get_user_by_email(email, app=app)
        except auth.UserNotFoundError:
            if not allow_create:
                raise RuntimeError("approved judge identity does not exist") from None
            user = auth.create_user(
                email=email,
                email_verified=True,
                password=password,
                disabled=False,
                app=app,
            )
        if user.disabled:
            raise RuntimeError("judge identity is disabled")
        if not user.email_verified:
            if not allow_create:
                raise RuntimeError("judge identity email is not verified")
            user = auth.update_user(user.uid, email_verified=True, app=app)
        return user
    finally:
        firebase_admin.delete_app(app)


def sign_in(
    *,
    api_key: str,
    origin: str,
    email: str,
    password: str,
) -> tuple[str, str]:
    response = httpx.post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword",
        params={"key": api_key},
        headers={"Origin": origin, "Referer": f"{origin}/"},
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError("judge identity sign-in failed")
    payload = response.json()
    return payload["localId"], payload["idToken"]


def scenario_client_version(scenario: RealScenarioSpec) -> str:
    if scenario.attempt == 1:
        return DATASET_CLIENT_VERSION
    return f"{DATASET_CLIENT_VERSION}-retry-{scenario.attempt}"


def scenario_idempotency_key(dataset_id: str, scenario: RealScenarioSpec) -> str:
    base = f"{dataset_id}-{scenario.key}"
    if scenario.attempt == 1:
        return f"{base}-v1"
    return f"{base}-retry-{scenario.attempt}"


def upload_browser_fixture(
    client: httpx.Client,
    *,
    camera_id: str,
    fixture: Path,
    captured_at: datetime,
    sequence_id: str,
    sequence_number: int,
    client_version: str,
    idempotency_key: str,
) -> tuple[str, bool]:
    from PIL import Image

    image_bytes = fixture.read_bytes()
    with Image.open(fixture) as image:
        width, height = image.size
    metadata = {
        "schema_version": 1,
        "camera_id": camera_id,
        "captured_at": captured_at.isoformat(),
        "client_kind": "browser",
        "client_version": client_version,
        "sequence_id": sequence_id,
        "sequence_number": sequence_number,
        "width": width,
        "height": height,
    }
    response = client.post(
        "/v1/captures",
        headers={"Idempotency-Key": idempotency_key},
        files={
            "metadata": (
                None,
                json.dumps(metadata, separators=(",", ":")),
                "application/json",
            ),
            "image": (fixture.name, image_bytes, "image/png"),
        },
        timeout=60,
    )
    if response.status_code != 202:
        raise RuntimeError("judge fixture upload failed")
    payload = response.json()
    return payload["capture_id"], bool(payload["duplicate"])


def validate_activity(
    activity: dict[str, object],
    *,
    scenario: RealScenarioSpec,
    context_note_id: str,
    previous_knowledge_revision_id: str | None,
) -> None:
    hypothesis = activity.get("activity_hypothesis")
    if not isinstance(hypothesis, dict):
        raise RuntimeError("judge activity lacks a structured hypothesis")
    if hypothesis.get("kind") != scenario.expected_kind:
        raise RuntimeError(f"judge scenario {scenario.key} produced the wrong activity kind")
    if (
        scenario.expected_confidence is not None
        and hypothesis.get("confidence") != scenario.expected_confidence
    ):
        raise RuntimeError(f"judge scenario {scenario.key} produced unsafe confidence")
    if scenario.key.startswith("ambiguous-with-context"):
        question = hypothesis.get("question")
        if not isinstance(question, dict) or len(question.get("candidate_labels") or []) < 2:
            raise RuntimeError("ambiguous judge scenario lacks a focused candidate question")
        context_ids = {
            item.get("source_id")
            for item in hypothesis.get("contextual_evidence", [])
            if isinstance(item, dict) and item.get("source_kind") == "user_note"
        }
        if context_note_id not in context_ids:
            raise RuntimeError("ambiguous judge scenario did not cite the synthetic context note")
    if scenario.must_cite_previous_learning:
        if previous_knowledge_revision_id is None:
            raise RuntimeError("learned follow-up ran before the correction")
        context_ids = {
            item.get("source_id")
            for item in hypothesis.get("contextual_evidence", [])
            if isinstance(item, dict) and item.get("source_kind") == "household_knowledge"
        }
        assumption_ids = {
            item.get("knowledge_revision_id")
            for item in hypothesis.get("assumptions", [])
            if isinstance(item, dict)
        }
        if previous_knowledge_revision_id not in context_ids | assumption_ids:
            raise RuntimeError("learned follow-up did not cite the exact correction revision")


async def prepare(args: argparse.Namespace) -> None:
    if not args.confirm_production_write:
        raise RuntimeError("pass --confirm-production-write after the exact action is approved")
    email = os.environ["FOODLOG_JUDGE_EMAIL"].strip().casefold()
    password = os.environ["FOODLOG_JUDGE_PASSWORD"]
    if not email.endswith(".invalid") or len(password) < 20:
        raise RuntimeError("judge credentials do not satisfy the approved safety contract")

    spec = load_dataset(args.manifest, args.fixture_root)
    identity = firebase_identity(
        project_id=args.project,
        email=email,
        password=password,
        allow_create=args.create_approved_identity,
    )
    owner_user_id, token = sign_in(
        api_key=args.firebase_api_key,
        origin=args.origin,
        email=email,
        password=password,
    )
    if owner_user_id != identity.uid:
        raise RuntimeError("Firebase Admin and REST identities disagree")

    repository = FirestoreRepository(
        project_id=args.project,
        public_account_limit=25,
        trial_image_limit=200,
        unlimited_owner_user_ids={owner_user_id},
    )
    store = GCSObjectStore(project_id=args.project, bucket_name=args.bucket)
    provisioning = AccountProvisioningService(
        repository=repository,
        publisher=PubSubNotificationPublisher(topic=args.notification_topic),
        public_account_limit=25,
    )
    try:
        account = await provisioning.provision_account(
            owner_user_id,
            verified_email_normalized=email,
        )
        if account.entitlement_mode != EntitlementMode.UNLIMITED:
            raise RuntimeError("judge account is not an internal unlimited account")
        notification = await repository._client.collection("outbox").document(
            f"account-created-{account.id}"
        ).get()
        if notification.get("status") not in {
            NotificationOutboxStatus.PUBLISHED.value,
            NotificationOutboxStatus.DELIVERING.value,
            NotificationOutboxStatus.DELIVERED.value,
        }:
            raise RuntimeError("judge account notification was not published")

        with httpx.Client(
            base_url=args.api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": args.origin,
                "Referer": f"{args.origin}/",
            },
            timeout=60,
        ) as client:
            visible_account = request_json(client, "POST", "/v1/accounts", expected_status=200)
            if not isinstance(visible_account, dict) or visible_account.get(
                "entitlement_mode"
            ) != EntitlementMode.UNLIMITED.value:
                raise RuntimeError("deployed API does not see the unlimited judge entitlement")

            camera_view = request_json(
                client,
                "POST",
                "/v1/browser-cameras",
                expected_status=200,
                json_body={
                    "name": "Reviewed synthetic judge dataset",
                    "client_instance_id": f"{spec.dataset_id}-browser-camera-v1",
                },
            )
            if not isinstance(camera_view, dict):
                raise RuntimeError("judge browser camera was not created")
            camera_id = camera_view["id"]
            camera = await repository.camera_for_owner(owner_user_id, camera_id)
            if not isinstance(camera, BrowserCamera):
                raise RuntimeError("judge dataset camera has the wrong kind")

            note = request_json(
                client,
                "POST",
                "/v1/context-notes",
                expected_status=201,
                headers={"Idempotency-Key": f"{spec.dataset_id}-context-note-v1"},
                json_body={
                    "text": spec.context_note.text,
                    "valid_from": spec.context_note.valid_from.isoformat(),
                    "valid_until": spec.context_note.valid_until.isoformat(),
                },
            )
            if not isinstance(note, dict):
                raise RuntimeError("judge context note was not persisted")

            knowledge_revision_id: str | None = None
            completed_scenarios: list[str] = []
            skipped_scenarios: list[str] = []
            for sequence_number, scenario in enumerate(
                spec.real_inference_scenarios,
                start=1,
            ):
                fixture = checked_fixture(args.fixture_root, scenario.fixture, scenario.sha256)
                current_traces = trace_ids(client)
                client_version = scenario_client_version(scenario)
                scenario_key = scenario_idempotency_key(spec.dataset_id, scenario)
                captures = request_json(
                    client,
                    "GET",
                    "/v1/captures?limit=200",
                    expected_status=200,
                )
                if not isinstance(captures, list):
                    raise RuntimeError("judge capture inventory is malformed")
                already_uploaded = any(
                    capture.get("content_sha256") == scenario.sha256
                    and isinstance(capture.get("metadata"), dict)
                    and capture["metadata"].get("client_version")
                    == client_version
                    and capture["metadata"].get("sequence_id") == spec.dataset_id
                    and capture["metadata"].get("sequence_number") == sequence_number
                    for capture in captures
                )
                if not already_uploaded and len(current_traces) > MAX_MODEL_TRACES - 2:
                    skipped_scenarios.extend(
                        item.key
                        for item in spec.real_inference_scenarios[sequence_number - 1 :]
                    )
                    break
                capture_id, duplicate = upload_browser_fixture(
                    client,
                    camera_id=camera_id,
                    fixture=fixture,
                    captured_at=scenario.captured_at,
                    sequence_id=spec.dataset_id,
                    sequence_number=sequence_number,
                    client_version=client_version,
                    idempotency_key=scenario_key,
                )
                if duplicate != already_uploaded:
                    raise RuntimeError("judge scenario upload inventory disagrees with idempotency")
                activity, _ = wait_for_activity(
                    client,
                    account_id=account.id,
                    capture_id=capture_id,
                    previous_trace_ids=set() if duplicate else current_traces,
                    timeout_seconds=args.timeout_seconds,
                )
                if len(trace_ids(client)) > MAX_MODEL_TRACES:
                    raise RuntimeError("judge dataset exceeded the approved model-call ceiling")
                validate_activity(
                    activity,
                    scenario=scenario,
                    context_note_id=note["id"],
                    previous_knowledge_revision_id=knowledge_revision_id,
                )
                if scenario.feedback is not None:
                    feedback = request_json(
                        client,
                        "POST",
                        f"/v1/meals/{activity['id']}/feedback",
                        expected_status=200,
                        headers={"Idempotency-Key": f"{scenario_key}-feedback"},
                        json_body={
                            "kind": "correct",
                            "actual_meal": scenario.feedback.actual_meal,
                            "explanation": scenario.feedback.explanation,
                            "learning_disposition": scenario.feedback.learning_disposition,
                        },
                    )
                    if not isinstance(feedback, dict) or feedback.get(
                        "learning_outcome"
                    ) != "knowledge_applied":
                        raise RuntimeError("judge correction did not create reusable knowledge")
                    knowledge_revision_id = feedback["knowledge"]["revision"]["id"]
                if scenario.discard_explanation is not None:
                    discarded = request_json(
                        client,
                        "POST",
                        f"/v1/meals/{activity['id']}/feedback",
                        expected_status=200,
                        headers={"Idempotency-Key": f"{scenario_key}-discard"},
                        json_body={
                            "kind": "not_cooking",
                            "explanation": scenario.discard_explanation,
                        },
                    )
                    if not isinstance(discarded, dict) or discarded.get(
                        "learning_outcome"
                    ) != "not_cooking":
                        raise RuntimeError("judge cat scenario was not discarded")
                completed_scenarios.append(scenario.key)
                remaining_calls = MAX_MODEL_TRACES - len(trace_ids(client))
                if remaining_calls < 2:
                    skipped_scenarios.extend(
                        item.key
                        for item in spec.real_inference_scenarios[sequence_number:]
                    )
                    break

            pattern = spec.synthetic_pattern_history
            pattern_fixture = checked_fixture(
                args.fixture_root,
                pattern.fixture,
                pattern.sha256,
            ).read_bytes()
            grouping = CaptureGroupingService(
                repository=repository,
                policy=GroupingPolicy(version=PATTERN_CLIENT_VERSION),
            )
            for ordinal, event in enumerate(pattern.events, start=1):
                await seed_synthetic_meal(
                    repository,
                    store,
                    grouping,
                    account=account,
                    camera=camera,
                    image=pattern_fixture,
                    local_at=event.captured_at,
                    title=event.title,
                    sequence_id=f"{spec.dataset_id}-pattern",
                    sequence_number=ordinal,
                    idempotency_key=f"{spec.dataset_id}-pattern-{ordinal}",
                    client_version=PATTERN_CLIENT_VERSION,
                    worker_id="judge-demo-synthetic-pattern",
                    lease_owner="judge-demo-synthetic-publication",
                    evidence_description=pattern.provenance_label,
                    rationale=(
                        "Explicitly synthetic no-model history prepared only to demonstrate "
                        "the longitudinal pattern-question UX."
                    ),
                    capture_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"{account.id}:{spec.dataset_id}:pattern:{ordinal}",
                        )
                    ),
                )

            questions = await PatternDetectionService(repository).detect_and_propose(
                account_id=account.id,
                max_proposals=5,
            )
            expected_questions = [
                question
                for question in questions
                if question.pattern_claim is not None
                and question.pattern_claim.value == pattern.expected_claim_value
                and pattern.expected_condition in question.pattern_claim.conditions
            ]
            if len(expected_questions) != 1:
                visible_questions = request_json(
                    client,
                    "GET",
                    "/v1/questions",
                    expected_status=200,
                )
                if not isinstance(visible_questions, list):
                    raise RuntimeError("judge pattern questions are not visible")
                expected_questions = [
                    question
                    for question in visible_questions
                    if isinstance(question.get("pattern_claim"), dict)
                    and question["pattern_claim"].get("value")
                    == pattern.expected_claim_value
                    and pattern.expected_condition
                    in question["pattern_claim"].get("conditions", [])
                ]
            if len(expected_questions) != 1:
                raise RuntimeError("judge Thursday pattern question was not created exactly once")

            journal = request_json(client, "GET", "/v1/journal", expected_status=200)
            discarded = request_json(
                client,
                "GET",
                "/v1/activities?status=not_cooking",
                expected_status=200,
            )
            notes = request_json(client, "GET", "/v1/context-notes", expected_status=200)
            if not all(isinstance(item, list) for item in (journal, discarded, notes)):
                raise RuntimeError("judge dataset final inventory is malformed")

            print("judge_identity_verified=true")
            print("judge_entitlement=unlimited")
            print(f"real_scenarios_completed={len(completed_scenarios)}")
            print(f"real_scenarios_skipped_for_call_cap={len(skipped_scenarios)}")
            print(f"model_trace_count={len(trace_ids(client))}")
            print(f"synthetic_pattern_meals={len(pattern.events)}")
            print("pattern_question_open=true")
            print(f"journal_entries={len(journal)}")
            print(f"discarded_entries={len(discarded)}")
            print(f"active_context_notes={len(notes)}")
            print("credentials_and_resource_ids_omitted=true")
    finally:
        repository._client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--notification-topic", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=300, choices=range(30, 601))
    parser.add_argument("--create-approved-identity", action="store_true")
    parser.add_argument("--confirm-production-write", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(prepare(parse_args()))
