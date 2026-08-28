from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_-]{0,63}$"),
]
BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class InferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InferenceKind(StrEnum):
    TENTATIVE_MEAL = "tentative_meal"
    UNKNOWN_ACTIVITY = "unknown_activity"
    LIKELY_NON_COOKING = "likely_non_cooking"


class InferenceConfidence(StrEnum):
    CONFIDENT = "confident"
    LIKELY = "likely"
    UNCERTAIN = "uncertain"


class UserAction(StrEnum):
    CONFIRM_GUESS = "confirm_guess"
    CORRECT = "correct"
    DISCARD_NOT_COOKING = "discard_not_cooking"


class QuestionImpact(StrEnum):
    CHANGES_MEAL_IDENTITY = "changes_meal_identity"
    CHANGES_FOOD_TRIGGER_RELEVANCE = "changes_food_trigger_relevance"
    CHANGES_REUSABLE_HOUSEHOLD_DISTINCTION = "changes_reusable_household_distinction"


class ContextSourceKind(StrEnum):
    PURCHASE = "purchase"
    HOUSEHOLD_KNOWLEDGE = "household_knowledge"
    RECENT_MEAL = "recent_meal"
    USER_NOTE = "user_note"


class ImageRegion(InferenceModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    # Vertex structured-output schemas support minimum, but not exclusiveMinimum.
    # The validator below retains the product's stricter positive-size invariant.
    width: float = Field(ge=0, le=1)
    height: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def stay_inside_image(self) -> ImageRegion:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("image region dimensions must be positive")
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("image region must stay inside normalized image bounds")
        return self


class ImageEvidenceLink(InferenceModel):
    capture_id: str = Field(min_length=1, max_length=160)
    region: ImageRegion | None = None


class DirectObservation(InferenceModel):
    id: Identifier
    description: BoundedText
    image_evidence: list[ImageEvidenceLink] = Field(min_length=1, max_length=12)


class ContextEvidence(InferenceModel):
    id: Identifier
    description: BoundedText
    source_kind: ContextSourceKind
    source_id: str = Field(min_length=1, max_length=160)


class ReasoningAssumption(InferenceModel):
    id: Identifier
    description: BoundedText
    knowledge_revision_id: str = Field(min_length=1, max_length=160)


class Deduction(InferenceModel):
    id: Identifier
    description: BoundedText
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=16)


class Alternative(InferenceModel):
    label: str = Field(min_length=1, max_length=160)
    reason: BoundedText
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=16)


class InferenceMealComponent(InferenceModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=160)
    ingredients: list[str] = Field(max_length=40)
    preparation_methods: list[str] = Field(max_length=20)
    confidence: InferenceConfidence
    alternatives: list[Alternative] = Field(max_length=8)
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=24)


class FocusedEventQuestion(InferenceModel):
    prompt: str = Field(min_length=1, max_length=240)
    justification: BoundedText
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=16)
    candidate_labels: list[str] = Field(min_length=2, max_length=8)
    impact: QuestionImpact

    @model_validator(mode="after")
    def reject_generic_labeling_question(self) -> FocusedEventQuestion:
        normalized = " ".join(self.prompt.casefold().split())
        forbidden = (
            "what meal",
            "which meal",
            "what ingredient",
            "what were you cooking",
            "what are you cooking",
        )
        if any(phrase in normalized for phrase in forbidden):
            raise ValueError("question must distinguish specific hypotheses, not request a label")
        normalized_labels = [" ".join(label.casefold().split()) for label in self.candidate_labels]
        if any(not label for label in normalized_labels):
            raise ValueError("question candidate labels cannot be blank")
        if len(set(normalized_labels)) != len(normalized_labels):
            raise ValueError("question candidate labels must be unique")
        return self


class ActivityMealInferenceV1(InferenceModel):
    """Shared validated contract for model output and persisted journal hypotheses."""

    schema_version: Literal["activity-meal-inference-v1"]
    event_id: str = Field(min_length=1, max_length=160)
    source_capture_ids: list[str] = Field(min_length=1, max_length=120)
    kind: InferenceKind
    best_guess: str | None = Field(default=None, min_length=1, max_length=200)
    confidence: InferenceConfidence
    components: list[InferenceMealComponent] = Field(max_length=20)
    direct_observations: list[DirectObservation] = Field(min_length=1, max_length=80)
    contextual_evidence: list[ContextEvidence] = Field(max_length=80)
    assumptions: list[ReasoningAssumption] = Field(max_length=40)
    deductions: list[Deduction] = Field(max_length=80)
    alternatives: list[Alternative] = Field(max_length=12)
    rationale: str = Field(min_length=1, max_length=2_000)
    question: FocusedEventQuestion | None = None
    allowed_actions: list[UserAction] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def enforce_state_and_evidence_invariants(self) -> ActivityMealInferenceV1:
        if len(set(self.source_capture_ids)) != len(self.source_capture_ids):
            raise ValueError("source capture IDs must be unique")

        observation_ids = [item.id for item in self.direct_observations]
        context_ids = [item.id for item in self.contextual_evidence]
        assumption_ids = [item.id for item in self.assumptions]
        deduction_ids = [item.id for item in self.deductions]
        all_ids = observation_ids + context_ids + assumption_ids + deduction_ids
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("observation, context, assumption, and deduction IDs must be unique")

        capture_ids = set(self.source_capture_ids)
        for observation in self.direct_observations:
            linked_capture_ids = [link.capture_id for link in observation.image_evidence]
            if len(set(linked_capture_ids)) != len(linked_capture_ids):
                raise ValueError("an observation cannot link the same capture more than once")
            if not set(linked_capture_ids) <= capture_ids:
                raise ValueError("visual evidence must link a declared source capture")

        base_evidence_ids = set(observation_ids + context_ids + assumption_ids)
        for deduction in self.deductions:
            self._require_known_references(deduction.evidence_ids, base_evidence_ids)
        all_evidence_ids = base_evidence_ids | set(deduction_ids)
        for component in self.components:
            self._require_known_references(component.evidence_ids, all_evidence_ids)
            for alternative in component.alternatives:
                self._require_known_references(alternative.evidence_ids, all_evidence_ids)
        for alternative in self.alternatives:
            self._require_known_references(alternative.evidence_ids, all_evidence_ids)
        if self.question:
            self._require_known_references(self.question.evidence_ids, all_evidence_ids)

        tentative_actions = [
            UserAction.CONFIRM_GUESS,
            UserAction.CORRECT,
            UserAction.DISCARD_NOT_COOKING,
        ]
        non_confirmable_actions = [UserAction.CORRECT, UserAction.DISCARD_NOT_COOKING]
        if self.kind == InferenceKind.TENTATIVE_MEAL:
            if self.best_guess is None or not self.components:
                raise ValueError("tentative meals require a best guess and at least one component")
            if self.allowed_actions != tentative_actions:
                raise ValueError(
                    "tentative meals must expose confirm, correct, and discard actions"
                )
        elif self.kind == InferenceKind.UNKNOWN_ACTIVITY:
            if self.best_guess is not None or self.components or self.alternatives:
                raise ValueError(
                    "unknown activity cannot invent a guess, component, or alternative"
                )
            if self.confidence != InferenceConfidence.UNCERTAIN:
                raise ValueError("unknown activity must be uncertain")
            if self.allowed_actions != non_confirmable_actions:
                raise ValueError("unknown activity cannot expose a confirmation action")
        else:
            if self.best_guess is None or self.components:
                raise ValueError(
                    "non-cooking activity requires a label and forbids meal components"
                )
            if self.allowed_actions != non_confirmable_actions:
                raise ValueError("non-cooking activity cannot expose a meal-confirmation action")

        if self.question:
            if self.kind != InferenceKind.TENTATIVE_MEAL:
                raise ValueError("only a tentative meal may ask a focused event question")
            if self.confidence != InferenceConfidence.UNCERTAIN:
                raise ValueError("a focused event question requires uncertain confidence")
            if not self.alternatives:
                raise ValueError("a focused event question requires at least one named alternative")
            if self.question.candidate_labels[0] != self.best_guess:
                raise ValueError("a focused event question must lead with the current best guess")
            alternatives_by_label = {
                alternative.label: alternative for alternative in self.alternatives
            }
            unknown_candidates = (
                set(self.question.candidate_labels[1:]) - alternatives_by_label.keys()
            )
            if unknown_candidates:
                raise ValueError(
                    "question candidates must be exact named alternatives: "
                    f"{sorted(unknown_candidates)}"
                )
            question_evidence = set(self.question.evidence_ids)
            for candidate_label in self.question.candidate_labels[1:]:
                candidate_evidence = set(alternatives_by_label[candidate_label].evidence_ids)
                if not candidate_evidence <= question_evidence:
                    raise ValueError(
                        "question evidence must include every cited alternative's evidence"
                    )
        return self

    def _enforce_new_output_context_identity_guard(self) -> None:
        """Reject unsafe new output without invalidating immutable historical records."""
        visual_ids = {item.id for item in self.direct_observations}
        contextual_ids = {
            item.id for item in [*self.contextual_evidence, *self.assumptions]
        }
        context_bridged_visuals = {
            deduction.id: set(deduction.evidence_ids) & visual_ids
            for deduction in self.deductions
            if set(deduction.evidence_ids) & visual_ids
            and set(deduction.evidence_ids) & contextual_ids
        }
        all_bridged_visuals = set().union(*context_bridged_visuals.values())

        def normalized_phrase(value: str) -> str:
            return " ".join(value.casefold().replace("-", " ").split())

        normalized_best_guess = normalized_phrase(self.best_guess or "")
        component_identity_phrases = {
            normalized_phrase(component.name) for component in self.components
        }

        def preserves_visible_component_identity(alternative: Alternative) -> bool:
            normalized_alternative = normalized_phrase(alternative.label)
            return any(
                f" {identity} " in f" {normalized_best_guess} "
                and f" {identity} " in f" {normalized_alternative} "
                for identity in component_identity_phrases
            )

        top_level_visual_tie = bool(all_bridged_visuals) and any(
            set(alternative.evidence_ids) & all_bridged_visuals
            and not preserves_visible_component_identity(alternative)
            for alternative in self.alternatives
        )
        if top_level_visual_tie:
            if self.confidence != InferenceConfidence.UNCERTAIN:
                raise ValueError(
                    "context cannot resolve a visually supported alternative at likely confidence"
                )
            if self.question is None:
                raise ValueError(
                    "a context-resolved visual tie requires a focused candidate question"
                )
        for component in self.components:
            component_bridged_visuals = set().union(
                *(
                    context_bridged_visuals[evidence_id]
                    for evidence_id in component.evidence_ids
                    if evidence_id in context_bridged_visuals
                )
            )
            if (
                component_bridged_visuals
                and any(
                    set(alternative.evidence_ids) & component_bridged_visuals
                    and normalized_phrase(component.name)
                    not in normalized_phrase(alternative.label)
                    for alternative in component.alternatives
                )
                and component.confidence != InferenceConfidence.UNCERTAIN
            ):
                raise ValueError(
                    "context cannot resolve a visually supported component alternative "
                    "at likely confidence"
                )

    @staticmethod
    def _require_known_references(references: list[str], known_ids: set[str]) -> None:
        if len(set(references)) != len(references):
            raise ValueError("evidence references must be unique")
        unknown = set(references) - known_ids
        if unknown:
            raise ValueError(f"unknown evidence references: {sorted(unknown)}")


_MODEL_SCHEMA_COMPLEXITY_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "title",
    }
)


def _model_facing_schema(value: Any) -> Any:
    """Remove constraints Vertex need not enforce; Pydantic still validates all of them."""
    if isinstance(value, dict):
        return {
            key: _model_facing_schema(item)
            for key, item in value.items()
            if key not in _MODEL_SCHEMA_COMPLEXITY_KEYWORDS
        }
    if isinstance(value, list):
        return [_model_facing_schema(item) for item in value]
    return value


class ActivityMealInferenceModelOutputV1(ActivityMealInferenceV1):
    """Strict inference result with a complexity-reduced Vertex response schema."""

    @model_validator(mode="after")
    def enforce_new_output_semantics(self) -> ActivityMealInferenceModelOutputV1:
        self._enforce_new_output_context_identity_guard()
        return self

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return _model_facing_schema(super().model_json_schema(*args, **kwargs))
