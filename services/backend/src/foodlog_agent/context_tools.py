from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Literal

from google.adk.tools import FunctionTool, ToolContext
from pydantic import BaseModel, ConfigDict, Field

from foodlog_backend.firestore_repository import FirestoreRepository
from foodlog_backend.models import (
    ClarificationQuestion,
    Confidence,
    MealEntry,
    MealStatus,
    PurchaseChargeKind,
    PurchaseDocumentKind,
    PurchaseEvidenceBundle,
    PurchaseItemDisposition,
    PurchaseReconciliationDisposition,
    QuestionEvidenceReference,
    QuestionKind,
    UserContextNote,
)
from foodlog_backend.repository import Repository

from .event_evidence_tool import ACCOUNT_ID_STATE_KEY
from .session_state import SessionStateContext, required_state_identifier

CONTEXT_TOOL_SCHEMA_VERSION = "agent-context-v1"
CONTEXT_TOOL_RESULT_LIMIT = 20
RECENT_PURCHASE_RESULT_LIMIT = 5
RECENT_PURCHASE_ITEM_LIMIT = 100


class RecentMealSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_id: str
    event_id: str
    occurred_at: str
    title: str = Field(max_length=200)
    status: MealStatus
    confidence: Confidence
    revision_number: int = Field(ge=1)
    components: list[BoundedMealComponentSummary] = Field(max_length=20)
    observations: list[str] = Field(max_length=20)
    alternatives: list[str] = Field(max_length=8)
    rationale: str = Field(max_length=2_000)


class BoundedMealComponentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=160)
    ingredients: list[str] = Field(max_length=40)
    preparation_methods: list[str] = Field(max_length=20)


class ActiveUserContextSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note_id: str
    text: str
    created_at: str
    valid_from: str | None
    valid_until: str | None


class UnresolvedMealSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meal_id: str
    event_id: str | None
    occurred_at: str
    title: str = Field(max_length=200)
    status: MealStatus
    confidence: Confidence
    revision_number: int = Field(ge=1)
    rationale: str = Field(max_length=2_000)


class OpenQuestionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    kind: QuestionKind
    meal_id: str | None
    event_id: str | None
    prompt: str
    reason: str
    evidence: list[QuestionEvidenceReference]
    choices: list[str]
    tentative_claim: str | None
    created_at: str


class PurchaseSourceDocumentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    kind: PurchaseDocumentKind
    revision_number: int = Field(ge=1)
    order_reference: str | None
    invoice_reference: str | None
    parser_version: str | None
    included_vat_ore: int | None
    created_at: str


class PurchaseItemSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    source_document_id: str
    source_revision_number: int = Field(ge=1)
    name: str
    normalized_name: str
    disposition: PurchaseItemDisposition
    quantity: int = Field(ge=1)
    category: str | None
    unit_description: str | None
    line_total_ore: int = Field(ge=0)


class PurchaseReconciliationItemSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reconciliation_item_id: str
    normalized_name: str
    display_name: str
    disposition: PurchaseReconciliationDisposition
    ordered_quantity: int | None
    delivered_quantity: int | None
    confirmation_item_ids: list[str]
    final_item_ids: list[str]


class PurchaseReconciliationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_document_id: str | None
    final_document_id: str | None
    item_count: int = Field(ge=1)
    unresolved_item_count: int = Field(ge=0)
    has_unresolved_substitution_pairing: bool
    items: list[PurchaseReconciliationItemSummary]
    items_truncated: bool
    updated_at: str


class RecentPurchaseSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purchase_id: str
    merchant: str
    revision_count: int = Field(ge=1)
    updated_at: str
    evidence_status: Literal["delivered", "ordered_only", "source_only"]
    source_documents: list[PurchaseSourceDocumentSummary]
    items: list[PurchaseItemSummary]
    items_truncated: bool
    latest_total_ore: int | None
    reconciliation: PurchaseReconciliationSummary | None


class RecentMealsToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTEXT_TOOL_SCHEMA_VERSION
    meals: list[RecentMealSummary]


class ActiveUserContextToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTEXT_TOOL_SCHEMA_VERSION
    notes: list[ActiveUserContextSummary]


class UnresolvedReviewsToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTEXT_TOOL_SCHEMA_VERSION
    meals: list[UnresolvedMealSummary]
    questions: list[OpenQuestionSummary]


class RecentPurchasesToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CONTEXT_TOOL_SCHEMA_VERSION
    available: bool
    unavailable_reason: Literal["no_purchase_evidence"] | None
    purchases: list[RecentPurchaseSummary]


def _recent_meal_summary(meal: MealEntry) -> RecentMealSummary:
    if meal.event_id is None:
        raise ValueError("Recent agent meal context requires event provenance")
    return RecentMealSummary(
        meal_id=meal.id,
        event_id=meal.event_id,
        occurred_at=(meal.occurred_at or meal.created_at).isoformat(),
        title=meal.title[:200],
        status=meal.status,
        confidence=meal.confidence,
        revision_number=meal.revision_number,
        components=[
            BoundedMealComponentSummary(
                name=component.name[:160],
                ingredients=[item[:500] for item in component.ingredients[:40]],
                preparation_methods=[
                    item[:500] for item in component.preparation_methods[:20]
                ],
            )
            for component in meal.components[:20]
        ],
        observations=[item[:500] for item in meal.observations[:20]],
        alternatives=[item[:500] for item in meal.alternatives[:8]],
        rationale=meal.rationale[:2_000],
    )


def _active_context_summary(note: UserContextNote) -> ActiveUserContextSummary:
    return ActiveUserContextSummary(
        note_id=note.id,
        text=note.text,
        created_at=note.created_at.isoformat(),
        valid_from=note.valid_from.isoformat() if note.valid_from is not None else None,
        valid_until=note.valid_until.isoformat() if note.valid_until is not None else None,
    )


def _unresolved_meal_summary(meal: MealEntry) -> UnresolvedMealSummary:
    return UnresolvedMealSummary(
        meal_id=meal.id,
        event_id=meal.event_id,
        occurred_at=(meal.occurred_at or meal.created_at).isoformat(),
        title=meal.title[:200],
        status=meal.status,
        confidence=meal.confidence,
        revision_number=meal.revision_number,
        rationale=meal.rationale[:2_000],
    )


def _open_question_summary(question: ClarificationQuestion) -> OpenQuestionSummary:
    return OpenQuestionSummary(
        question_id=question.id,
        kind=question.kind,
        meal_id=question.meal_id,
        event_id=question.event_id,
        prompt=question.prompt,
        reason=question.reason,
        evidence=question.evidence,
        choices=question.choices,
        tentative_claim=question.tentative_claim,
        created_at=question.created_at.isoformat(),
    )


def _recent_purchase_summary(bundle: PurchaseEvidenceBundle) -> RecentPurchaseSummary:
    normalizations = {
        normalization.document_id: normalization
        for normalization in bundle.normalizations
    }
    source_documents = [
        PurchaseSourceDocumentSummary(
            document_id=document.id,
            kind=document.kind,
            revision_number=document.revision_number,
            order_reference=document.order_reference,
            invoice_reference=document.invoice_reference,
            parser_version=(
                normalizations[document.id].parser_version
                if document.id in normalizations
                else None
            ),
            included_vat_ore=(
                normalizations[document.id].included_vat_ore
                if document.id in normalizations
                else None
            ),
            created_at=document.created_at.isoformat(),
        )
        for document in bundle.documents
    ]
    evidence_document_id = (
        bundle.purchase.latest_final_document_id
        or bundle.purchase.latest_confirmation_document_id
    )
    evidence_items = [
        item for item in bundle.items if item.document_id == evidence_document_id
    ]
    if not evidence_items:
        evidence_status: Literal["delivered", "ordered_only", "source_only"] = (
            "source_only"
        )
    elif evidence_document_id == bundle.purchase.latest_final_document_id:
        evidence_status = "delivered"
    else:
        evidence_status = "ordered_only"
    bounded_items = evidence_items[:RECENT_PURCHASE_ITEM_LIMIT]
    latest_total = next(
        (
            charge.amount_ore
            for charge in bundle.charges
            if charge.document_id == evidence_document_id
            and charge.kind == PurchaseChargeKind.TOTAL
        ),
        None,
    )
    reconciliation = bundle.reconciliation
    reconciliation_summary = None
    if reconciliation is not None:
        bounded_reconciliation_items = reconciliation.items[:RECENT_PURCHASE_ITEM_LIMIT]
        reconciliation_summary = PurchaseReconciliationSummary(
            confirmation_document_id=reconciliation.confirmation_document_id,
            final_document_id=reconciliation.final_document_id,
            item_count=reconciliation.item_count,
            unresolved_item_count=reconciliation.unresolved_item_count,
            has_unresolved_substitution_pairing=(
                reconciliation.has_unresolved_substitution_pairing
            ),
            items=[
                PurchaseReconciliationItemSummary(
                    reconciliation_item_id=item.id,
                    normalized_name=item.normalized_name,
                    display_name=item.display_name,
                    disposition=item.disposition,
                    ordered_quantity=item.ordered_quantity,
                    delivered_quantity=item.delivered_quantity,
                    confirmation_item_ids=item.confirmation_item_ids,
                    final_item_ids=item.final_item_ids,
                )
                for item in bounded_reconciliation_items
            ],
            items_truncated=(
                len(reconciliation.items) > RECENT_PURCHASE_ITEM_LIMIT
            ),
            updated_at=reconciliation.updated_at.isoformat(),
        )
    return RecentPurchaseSummary(
        purchase_id=bundle.purchase.id,
        merchant=bundle.purchase.merchant,
        revision_count=bundle.purchase.revision_count,
        updated_at=bundle.purchase.updated_at.isoformat(),
        evidence_status=evidence_status,
        source_documents=source_documents,
        items=[
            PurchaseItemSummary(
                item_id=item.id,
                source_document_id=item.document_id,
                source_revision_number=item.document_revision_number,
                name=item.name,
                normalized_name=item.normalized_name,
                disposition=item.disposition,
                quantity=item.quantity,
                category=item.category,
                unit_description=item.unit_description,
                line_total_ore=item.line_total_ore,
            )
            for item in bounded_items
        ],
        items_truncated=len(evidence_items) > RECENT_PURCHASE_ITEM_LIMIT,
        latest_total_ore=latest_total,
        reconciliation=reconciliation_summary,
    )


class ContextToolsService:
    def __init__(self, *, repository: Repository) -> None:
        self._repository = repository

    async def get_recent_meals(
        self,
        *,
        context: SessionStateContext,
    ) -> RecentMealsToolResult:
        account_id = required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        meals = await self._repository.recent_meals_for_account(
            account_id=account_id,
            limit=CONTEXT_TOOL_RESULT_LIMIT,
        )
        return RecentMealsToolResult(meals=[_recent_meal_summary(meal) for meal in meals])

    async def get_active_user_context(
        self,
        *,
        context: SessionStateContext,
    ) -> ActiveUserContextToolResult:
        account_id = required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        notes = await self._repository.active_user_context_notes_for_account(
            account_id=account_id,
            limit=CONTEXT_TOOL_RESULT_LIMIT,
        )
        return ActiveUserContextToolResult(
            notes=[_active_context_summary(note) for note in notes]
        )

    async def get_recent_purchases(
        self,
        *,
        context: SessionStateContext,
    ) -> RecentPurchasesToolResult:
        account_id = required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        bundles = await self._repository.recent_purchase_evidence_for_account(
            account_id=account_id,
            limit=RECENT_PURCHASE_RESULT_LIMIT,
        )
        purchases = [_recent_purchase_summary(bundle) for bundle in bundles]
        return RecentPurchasesToolResult(
            available=bool(purchases),
            unavailable_reason=None if purchases else "no_purchase_evidence",
            purchases=purchases,
        )

    async def get_unresolved_reviews(
        self,
        *,
        context: SessionStateContext,
    ) -> UnresolvedReviewsToolResult:
        account_id = required_state_identifier(context, ACCOUNT_ID_STATE_KEY)
        meals, questions = await self._repository.unresolved_reviews_for_account(
            account_id=account_id,
            limit=CONTEXT_TOOL_RESULT_LIMIT,
        )
        return UnresolvedReviewsToolResult(
            meals=[_unresolved_meal_summary(meal) for meal in meals],
            questions=[_open_question_summary(question) for question in questions],
        )


def build_context_tools(*, repository: Repository) -> list[FunctionTool]:
    service = ContextToolsService(repository=repository)

    async def get_recent_meals(tool_context: ToolContext) -> dict[str, Any]:
        """Return bounded recent meal evidence for the current account.

        Account scope comes only from trusted session state. The result excludes activity
        discarded as not cooking and retains meal, event, revision, and timing provenance.
        """
        result = await service.get_recent_meals(context=tool_context)
        return result.model_dump(mode="json")

    async def get_active_user_context(tool_context: ToolContext) -> dict[str, Any]:
        """Return the current account's active time-bounded user notes.

        Account scope comes only from trusted session state. Future, expired, and retired
        notes are excluded; note IDs and validity windows remain visible as provenance.
        """
        result = await service.get_active_user_context(context=tool_context)
        return result.model_dump(mode="json")

    async def get_recent_purchases(tool_context: ToolContext) -> dict[str, Any]:
        """Return bounded recent purchase evidence for the current account.

        Account scope comes only from trusted session state. Final receipt items take
        precedence over ordered items, while source IDs, revisions, and unresolved
        reconciliation uncertainty remain explicit. An empty result reports that purchase
        context is unavailable instead of implying that no relevant ingredient exists.
        """
        result = await service.get_recent_purchases(context=tool_context)
        return result.model_dump(mode="json")

    async def get_unresolved_reviews(tool_context: ToolContext) -> dict[str, Any]:
        """Return bounded unresolved meal and question evidence for the current account.

        Account scope comes only from trusted session state. Only provisional or contradicted
        meals and open questions are returned, with stable provenance identifiers.
        """
        result = await service.get_unresolved_reviews(context=tool_context)
        return result.model_dump(mode="json")

    return [
        FunctionTool(func=get_recent_meals),
        FunctionTool(func=get_recent_purchases),
        FunctionTool(func=get_active_user_context),
        FunctionTool(func=get_unresolved_reviews),
    ]


@lru_cache(maxsize=1)
def production_context_tools_service() -> ContextToolsService:
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for agent context tools")
    repository = FirestoreRepository(
        project_id=project_id,
        public_account_limit=25,
        trial_image_limit=200,
    )
    return ContextToolsService(repository=repository)


async def get_recent_meals(tool_context: ToolContext) -> dict[str, Any]:
    """Return bounded recent meal evidence for the current account.

    Account scope comes only from trusted session state. Activity discarded as not cooking
    is excluded; returned event and revision IDs are the provenance available for citation.
    """
    result = await production_context_tools_service().get_recent_meals(
        context=tool_context
    )
    return result.model_dump(mode="json")


async def get_active_user_context(tool_context: ToolContext) -> dict[str, Any]:
    """Return the current account's active time-bounded user notes.

    Account scope comes only from trusted session state. Future, expired, and retired notes
    are excluded; returned note IDs and validity windows are the provenance for citation.
    """
    result = await production_context_tools_service().get_active_user_context(
        context=tool_context
    )
    return result.model_dump(mode="json")


async def get_recent_purchases(tool_context: ToolContext) -> dict[str, Any]:
    """Return bounded recent purchase evidence for the current account.

    Account scope comes only from trusted session state. Final receipts are authoritative,
    source provenance is retained, and absent purchase context is explicit.
    """
    result = await production_context_tools_service().get_recent_purchases(
        context=tool_context
    )
    return result.model_dump(mode="json")


async def get_unresolved_reviews(tool_context: ToolContext) -> dict[str, Any]:
    """Return unresolved meal and question evidence for the current account.

    Account scope comes only from trusted session state. Only provisional or contradicted
    meals and open questions are returned, with stable provenance identifiers.
    """
    result = await production_context_tools_service().get_unresolved_reviews(
        context=tool_context
    )
    return result.model_dump(mode="json")


context_tools = [
    FunctionTool(func=get_recent_meals),
    FunctionTool(func=get_recent_purchases),
    FunctionTool(func=get_active_user_context),
    FunctionTool(func=get_unresolved_reviews),
]
