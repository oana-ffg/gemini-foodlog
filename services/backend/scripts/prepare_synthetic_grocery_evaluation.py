from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    EntitlementMode,
    ParsedPurchaseDocument,
    PurchaseChargeDraft,
    PurchaseChargeKind,
    PurchaseDocumentCandidate,
    PurchaseDocumentKind,
    PurchaseEvidenceOrigin,
    PurchaseItemDisposition,
    PurchaseItemDraft,
)
from foodlog_backend.notifications import (
    AccountProvisioningService,
    PubSubNotificationPublisher,
)
from foodlog_backend.repository import Repository
from scripts.prepare_judge_dataset import firebase_identity, sign_in
from scripts.production_smoke_support import request_json, trace_ids, wait_for_activity

PARSER_VERSION = "synthetic-grocery-evaluation-v1"
MAX_MODEL_TRACES = 12


class GroceryItemSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=240)
    quantity: int = Field(ge=1, le=1_000)
    category: str = Field(min_length=1, max_length=120)
    line_total_ore: int = Field(ge=0, le=100_000_000)


class PurchaseRevisionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recorded_at: datetime
    items: list[GroceryItemSpec] = Field(min_length=1, max_length=250)
    total_ore: int = Field(ge=0, le=100_000_000)
    included_vat_ore: int | None = Field(default=None, ge=0, le=100_000_000)

    @model_validator(mode="after")
    def timestamp_has_offset(self) -> PurchaseRevisionSpec:
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("synthetic purchase timestamp requires a UTC offset")
        return self


class FinalPurchaseRevisionSpec(PurchaseRevisionSpec):
    invoice_reference: str = Field(min_length=1, max_length=128)


class GroceryOrderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9-]+$")
    order_reference: str = Field(min_length=1, max_length=128)
    confirmation: PurchaseRevisionSpec
    final: FinalPurchaseRevisionSpec | None

    @model_validator(mode="after")
    def lifecycle_is_ordered(self) -> GroceryOrderSpec:
        if self.final is not None and self.final.recorded_at <= self.confirmation.recorded_at:
            raise ValueError("synthetic final receipt must follow its confirmation")
        return self


class GroceryScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9-]+$")
    attempt: int = Field(default=1, ge=1, le=20)
    retry_offset_minutes: int = Field(default=0, ge=0, le=720)
    fixture: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime
    expected_purchase_key: str = Field(pattern=r"^[a-z0-9-]+$")
    expected_kind: Literal["tentative_meal"]
    expected_confidence: Literal["uncertain"]
    question_policy: Literal["required", "allowed", "forbidden"]
    candidate_terms: list[str] = Field(min_length=1, max_length=8)
    forbidden_asserted_terms: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def timestamp_has_offset(self) -> GroceryScenarioSpec:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("synthetic scenario timestamp requires a UTC offset")
        if self.attempt == 1 and self.retry_offset_minutes != 0:
            raise ValueError("a primary scenario cannot have a retry time offset")
        return self


class GroceryEvaluationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    dataset_id: str = Field(pattern=r"^[a-z0-9-]+$")
    provenance_label: str = Field(min_length=1, max_length=500)
    orders: list[GroceryOrderSpec] = Field(min_length=1, max_length=20)
    scenarios: list[GroceryScenarioSpec] = Field(min_length=1, max_length=12)

    @field_validator("provenance_label")
    @classmethod
    def provenance_is_unambiguous(cls, value: str) -> str:
        normalized = value.casefold()
        if "synthetic" not in normalized or "not an authenticated" not in normalized:
            raise ValueError("evaluation provenance must reject authenticated-source status")
        return value

    @model_validator(mode="after")
    def references_are_complete(self) -> GroceryEvaluationSpec:
        order_keys = [order.key for order in self.orders]
        scenario_keys = [scenario.key for scenario in self.scenarios]
        if len(order_keys) != len(set(order_keys)):
            raise ValueError("synthetic grocery order keys must be unique")
        if len(scenario_keys) != len(set(scenario_keys)):
            raise ValueError("synthetic grocery scenario keys must be unique")
        if any(scenario.expected_purchase_key not in order_keys for scenario in self.scenarios):
            raise ValueError("synthetic grocery scenario references an unknown order")
        order_by_key = {order.key: order for order in self.orders}
        for scenario in self.scenarios:
            order = order_by_key[scenario.expected_purchase_key]
            if order.confirmation.recorded_at >= scenario.captured_at:
                raise ValueError("scenario must occur after its expected purchase evidence")
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


def load_dataset(path: Path, fixture_root: Path) -> GroceryEvaluationSpec:
    spec = GroceryEvaluationSpec.model_validate_json(path.read_text())
    for scenario in spec.scenarios:
        checked_fixture(fixture_root, scenario.fixture, scenario.sha256)
    return spec


def _document_identity(dataset_id: str, order_key: str, kind: str) -> tuple[str, str]:
    identity = f"{dataset_id}\0{order_key}\0{kind}"
    raw_mail_id = sha256(f"synthetic-document\0{identity}".encode()).hexdigest()
    content_sha256 = sha256(f"synthetic-content\0{identity}".encode()).hexdigest()
    return raw_mail_id, content_sha256


def _parsed_document(
    revision: PurchaseRevisionSpec,
    *,
    kind: PurchaseDocumentKind,
) -> ParsedPurchaseDocument:
    disposition = (
        PurchaseItemDisposition.ORDERED
        if kind == PurchaseDocumentKind.ORDER_CONFIRMATION
        else PurchaseItemDisposition.DELIVERED
    )
    return ParsedPurchaseDocument(
        parser_version=PARSER_VERSION,
        kind=kind,
        items=[
            PurchaseItemDraft(
                ordinal=ordinal,
                name=item.name,
                normalized_name=" ".join(item.name.casefold().split()),
                disposition=disposition,
                quantity=item.quantity,
                category=item.category,
                unit_description=f"{item.quantity} synthetic evaluation unit(s)",
                unit_price_ore=item.line_total_ore // item.quantity,
                line_total_ore=item.line_total_ore,
            )
            for ordinal, item in enumerate(revision.items, start=1)
        ],
        charges=[
            PurchaseChargeDraft(
                kind=PurchaseChargeKind.TOTAL,
                amount_ore=revision.total_ore,
                description="Explicitly synthetic evaluation total",
            )
        ],
        included_vat_ore=revision.included_vat_ore,
    )


async def seed_synthetic_purchases(
    repository: Repository,
    *,
    account_id: str,
    spec: GroceryEvaluationSpec,
) -> dict[str, str]:
    purchase_ids: dict[str, str] = {}
    for order in spec.orders:
        revisions: list[tuple[str, PurchaseDocumentKind, PurchaseRevisionSpec, str | None]] = [
            (
                "confirmation",
                PurchaseDocumentKind.ORDER_CONFIRMATION,
                order.confirmation,
                None,
            )
        ]
        if order.final is not None:
            revisions.append(
                (
                    "final",
                    PurchaseDocumentKind.FINAL_RECEIPT,
                    order.final,
                    order.final.invoice_reference,
                )
            )
        for identity_kind, document_kind, revision, invoice_reference in revisions:
            raw_mail_id, content_sha256 = _document_identity(
                spec.dataset_id,
                order.key,
                identity_kind,
            )
            candidate = PurchaseDocumentCandidate(
                account_id=account_id,
                raw_mail_id=raw_mail_id,
                raw_content_sha256=content_sha256,
                evidence_origin=PurchaseEvidenceOrigin.SYNTHETIC_EVALUATION,
                kind=document_kind,
                order_reference=order.order_reference,
                invoice_reference=invoice_reference,
            )
            identity = await repository.attach_synthetic_purchase_document(
                candidate,
                recorded_at=revision.recorded_at,
            )
            normalized = await repository.normalize_purchase_document(
                document=identity.document,
                parsed=_parsed_document(revision, kind=document_kind),
            )
            if identity.duplicate != normalized.duplicate:
                raise RuntimeError("synthetic purchase identity and normalization disagree")
            existing_purchase_id = purchase_ids.setdefault(order.key, identity.purchase.id)
            if existing_purchase_id != identity.purchase.id:
                raise RuntimeError("synthetic purchase revisions split across lifecycles")
            if identity.purchase.evidence_origin != PurchaseEvidenceOrigin.SYNTHETIC_EVALUATION:
                raise RuntimeError("synthetic purchase lost its evidence provenance")
    return purchase_ids


def upload_fixture(
    client: httpx.Client,
    *,
    camera_id: str,
    fixture: Path,
    scenario: GroceryScenarioSpec,
    dataset_id: str,
    sequence_number: int,
) -> tuple[str, bool]:
    image_bytes = fixture.read_bytes()
    with Image.open(fixture) as image:
        width, height = image.size
    response = client.post(
        "/v1/captures",
        headers={"Idempotency-Key": scenario_idempotency_key(dataset_id, scenario)},
        files={
            "metadata": (
                None,
                json.dumps(
                    {
                        "schema_version": 1,
                        "camera_id": camera_id,
                        "captured_at": scenario_capture_time(scenario).isoformat(),
                        "client_kind": "browser",
                        "client_version": scenario_client_version(dataset_id, scenario),
                        "sequence_id": dataset_id,
                        "sequence_number": sequence_number,
                        "width": width,
                        "height": height,
                    },
                    separators=(",", ":"),
                ),
                "application/json",
            ),
            "image": (fixture.name, image_bytes, "image/png"),
        },
        timeout=60,
    )
    if response.status_code != 202:
        raise RuntimeError("synthetic grocery fixture upload failed")
    payload = response.json()
    return payload["capture_id"], bool(payload["duplicate"])


def scenario_client_version(dataset_id: str, scenario: GroceryScenarioSpec) -> str:
    if scenario.attempt == 1:
        return dataset_id
    return f"{dataset_id}-retry-{scenario.attempt}"


def scenario_idempotency_key(dataset_id: str, scenario: GroceryScenarioSpec) -> str:
    base = f"{dataset_id}-{scenario.key}"
    if scenario.attempt == 1:
        return f"{base}-v1"
    return f"{base}-retry-{scenario.attempt}"


def scenario_capture_time(scenario: GroceryScenarioSpec) -> datetime:
    return scenario.captured_at + timedelta(minutes=scenario.retry_offset_minutes)


def validate_activity(
    activity: dict[str, object],
    *,
    scenario: GroceryScenarioSpec,
    expected_purchase_id: str,
    future_purchase_ids: set[str],
) -> dict[str, object]:
    hypothesis = activity.get("activity_hypothesis")
    if not isinstance(hypothesis, dict):
        raise RuntimeError("synthetic grocery activity lacks a structured hypothesis")
    if hypothesis.get("kind") != scenario.expected_kind:
        raise RuntimeError(f"{scenario.key} produced the wrong activity kind")
    if hypothesis.get("confidence") != scenario.expected_confidence:
        raise RuntimeError(f"{scenario.key} produced unsafe confidence")

    context = [
        item
        for item in hypothesis.get("contextual_evidence", [])
        if isinstance(item, dict) and item.get("source_kind") == "purchase"
    ]
    cited_ids = {item.get("source_id") for item in context}
    if expected_purchase_id not in cited_ids:
        raise RuntimeError(f"{scenario.key} did not cite its relevant synthetic purchase")
    if cited_ids & future_purchase_ids:
        raise RuntimeError(f"{scenario.key} cited purchase evidence from the future")
    expected_evidence = next(
        item for item in context if item.get("source_id") == expected_purchase_id
    )
    if "synthetic" not in str(expected_evidence.get("description", "")).casefold():
        raise RuntimeError(f"{scenario.key} hid the purchase's synthetic provenance")

    question = hypothesis.get("question")
    if scenario.question_policy == "required" and not isinstance(question, dict):
        raise RuntimeError(f"{scenario.key} omitted its focused material question")
    if scenario.question_policy == "forbidden" and question is not None:
        raise RuntimeError(f"{scenario.key} asked a forbidden question")
    labels = [
        str(hypothesis.get("best_guess") or ""),
        *[
            str(item.get("label") or "")
            for item in hypothesis.get("alternatives", [])
            if isinstance(item, dict)
        ],
    ]
    if isinstance(question, dict):
        labels.extend(str(item) for item in question.get("candidate_labels", []))
        labels.append(str(question.get("prompt") or ""))
    for component in hypothesis.get("components", []):
        if not isinstance(component, dict):
            continue
        labels.extend(
            (
                str(component.get("name") or ""),
                *[str(item) for item in component.get("ingredients", [])],
                *[
                    str(item.get("label") or "")
                    for item in component.get("alternatives", [])
                    if isinstance(item, dict)
                ],
            )
        )
    normalized_labels = " ".join(labels).casefold()
    matched_terms = {
        term for term in scenario.candidate_terms if term.casefold() in normalized_labels
    }
    required_terms = 2 if scenario.question_policy == "required" else 1
    if len(matched_terms) < required_terms:
        raise RuntimeError(f"{scenario.key} did not preserve the expected candidates")

    user_facing_text = json.dumps(hypothesis, sort_keys=True).casefold()
    if any(
        _contains_unqualified_claim(user_facing_text, term.casefold())
        for term in scenario.forbidden_asserted_terms
    ):
        raise RuntimeError(f"{scenario.key} asserted unavailable synthetic groceries")
    return {
        "scenario": scenario.key,
        "kind": hypothesis.get("kind"),
        "confidence": hypothesis.get("confidence"),
        "best_guess": hypothesis.get("best_guess"),
        "question_asked": isinstance(question, dict),
        "purchase_cited": True,
        "synthetic_provenance_visible": True,
        "future_purchase_leak": False,
    }


def _contains_unqualified_claim(text: str, phrase: str) -> bool:
    search_from = 0
    negations = ("not ", "no evidence ", "cannot ", "can't ", "unconfirmed ")
    while (index := text.find(phrase, search_from)) >= 0:
        prefix = text[max(0, index - 48) : index]
        if not any(marker in prefix for marker in negations):
            return True
        search_from = index + len(phrase)
    return False


async def _usage_for_events(
    repository: FirestoreRepository,
    *,
    account_id: str,
    event_ids: set[str],
) -> tuple[int, int]:
    snapshots = repository._collection(account_id, "model_usage").stream()
    records = [snapshot.to_dict() async for snapshot in snapshots]
    relevant = [record for record in records if record.get("event_id") in event_ids]
    return len(relevant), sum(int(record.get("actual_dkk_micros", 0)) for record in relevant)


async def prepare(args: argparse.Namespace) -> None:
    if not args.confirm_production_write:
        raise RuntimeError("pass --confirm-production-write after the exact action is approved")
    email = os.environ["FOODLOG_EVALUATION_EMAIL"].strip().casefold()
    password = os.environ["FOODLOG_EVALUATION_PASSWORD"]
    if not email.endswith(".invalid") or len(password) < 20:
        raise RuntimeError("evaluation credentials do not satisfy the safety contract")

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
            raise RuntimeError("evaluation account is not an internal unlimited account")
        purchase_ids = await seed_synthetic_purchases(
            repository,
            account_id=account.id,
            spec=spec,
        )

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
            if not isinstance(visible_account, dict):
                raise RuntimeError("deployed API did not return the evaluation account")
            camera = request_json(
                client,
                "POST",
                "/v1/browser-cameras",
                expected_status=200,
                json_body={
                    "name": "Synthetic grocery longitudinal evaluation",
                    "client_instance_id": f"{spec.dataset_id}-camera-v1",
                },
            )
            if not isinstance(camera, dict):
                raise RuntimeError("evaluation browser camera was not created")

            scenario_results: list[dict[str, object]] = []
            event_ids: set[str] = set()
            ordered_scenarios = sorted(spec.scenarios, key=lambda item: item.captured_at)
            for sequence_number, scenario in enumerate(ordered_scenarios, start=1):
                current_traces = trace_ids(client)
                if len(current_traces) > MAX_MODEL_TRACES - 2:
                    raise RuntimeError("evaluation account reached its bounded trace ceiling")
                fixture = checked_fixture(args.fixture_root, scenario.fixture, scenario.sha256)
                capture_id, duplicate = upload_fixture(
                    client,
                    camera_id=str(camera["id"]),
                    fixture=fixture,
                    scenario=scenario,
                    dataset_id=spec.dataset_id,
                    sequence_number=sequence_number,
                )
                activity, trace_id = wait_for_activity(
                    client,
                    account_id=account.id,
                    capture_id=capture_id,
                    previous_trace_ids=set() if duplicate else current_traces,
                    timeout_seconds=args.timeout_seconds,
                )
                event_id = activity.get("event_id")
                if not isinstance(event_id, str):
                    raise RuntimeError("evaluation activity omitted its event identity")
                event_ids.add(event_id)
                future_purchase_ids = {
                    purchase_ids[order.key]
                    for order in spec.orders
                    if order.confirmation.recorded_at > scenario_capture_time(scenario)
                }
                result = validate_activity(
                    activity,
                    scenario=scenario,
                    expected_purchase_id=purchase_ids[scenario.expected_purchase_key],
                    future_purchase_ids=future_purchase_ids,
                )
                result["trace_id"] = trace_id
                result["duplicate_capture"] = duplicate
                result["attempt"] = scenario.attempt
                result["elapsed_simulated_days"] = (
                    scenario.captured_at - ordered_scenarios[0].captured_at
                ).days
                result["eligible_purchase_histories"] = sum(
                    order.confirmation.recorded_at <= scenario_capture_time(scenario)
                    for order in spec.orders
                )
                result["prior_inferred_events"] = len(scenario_results)
                scenario_results.append(result)
                print(
                    json.dumps({"scenario_result": result}, sort_keys=True),
                    flush=True,
                )

            usage_count, actual_dkk_micros = await _usage_for_events(
                repository,
                account_id=account.id,
                event_ids=event_ids,
            )
            report = {
                "dataset_id": spec.dataset_id,
                "simulated_days": (
                    ordered_scenarios[-1].captured_at - ordered_scenarios[0].captured_at
                ).days,
                "synthetic_orders": len(spec.orders),
                "scenarios": scenario_results,
                "model_usage_records": usage_count,
                "actual_dkk_micros": actual_dkk_micros,
                "actual_dkk": actual_dkk_micros / 1_000_000,
                "credentials_omitted": True,
            }
            print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        repository._client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--firebase-api-key", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--notification-topic", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=300, choices=range(30, 601))
    parser.add_argument("--create-approved-identity", action="store_true")
    parser.add_argument("--confirm-production-write", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(prepare(parse_args()))
