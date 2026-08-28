from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    ClarificationQuestion,
    Confidence,
    KnowledgeClaim,
    MealEntry,
    MealRevision,
    MealStatus,
    PatternEvidenceExample,
    QuestionEvidenceReference,
    QuestionStatus,
)
from .repository import Repository

PATTERN_DETECTION_VERSION = "longitudinal-pattern-v1"
PATTERN_HISTORY_LIMIT = 100
MINIMUM_SUPPORTING_EXAMPLES = 3
MINIMUM_DISTINCT_WEEKS = 3
MINIMUM_OBSERVATION_SPAN = timedelta(days=14)
MINIMUM_SUPPORT_RATIO = 0.75
MAXIMUM_PATTERN_EVIDENCE = 20

_TOKEN_PATTERN = re.compile(r"[\w'-]+", re.UNICODE)
_VALUE_STOPWORDS = frozenset({"a", "an", "and", "food", "meal", "or", "the", "usually", "with"})
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_DAY_GROUPS = {"weekday", "weekdays", "weekend", "weekends"}
_MEAL_PERIODS = {
    "breakfast": (time(4), time(11)),
    "lunch": (time(11), time(16)),
    "dinner": (time(16), time(23, 59, 59, 999999)),
}


class PatternCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    statement: str = Field(min_length=1, max_length=2_000)
    claim: KnowledgeClaim
    supporting_revision_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    counterexample_revision_ids: tuple[str, ...] = Field(default=(), max_length=20)
    uncertainty: str = Field(min_length=1, max_length=1_000)

    @field_validator("supporting_revision_ids", "counterexample_revision_ids")
    @classmethod
    def evidence_ids_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("pattern evidence IDs must be unique")
        return values

    @model_validator(mode="after")
    def support_and_counterexamples_are_disjoint(self) -> PatternCandidate:
        if set(self.supporting_revision_ids) & set(self.counterexample_revision_ids):
            raise ValueError("support and counterexample evidence must be disjoint")
        return self


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_PATTERN.findall(value)}


def _meal_tokens(meal: MealEntry) -> set[str]:
    values = [meal.title, *meal.observations, *meal.alternatives]
    for component in meal.components:
        values.extend([component.name, *component.ingredients, *component.preparation_methods])
    return set().union(*(_tokens(value) for value in values))


def _value_label_token_sets(value: str) -> tuple[set[str], ...]:
    return tuple(
        tokens
        for label in re.split(r"\s+or\s+", value, flags=re.IGNORECASE)
        if (tokens := _tokens(label) - _VALUE_STOPWORDS)
    )


def _local_occurrence(meal: MealEntry) -> datetime:
    occurred_at = meal.occurred_at or meal.created_at
    if meal.occurred_utc_offset_minutes is None:
        raise ValueError("pattern evidence lacks its captured local UTC offset")
    return occurred_at.astimezone(timezone(timedelta(minutes=meal.occurred_utc_offset_minutes)))


def _recognized_temporal_conditions(claim: KnowledgeClaim) -> set[str]:
    tokens = set().union(*(_tokens(condition) for condition in claim.conditions))
    return tokens & (set(_WEEKDAYS) | _DAY_GROUPS | set(_MEAL_PERIODS))


def _matches_temporal_conditions(local_at: datetime, conditions: set[str]) -> bool:
    for label, weekday in _WEEKDAYS.items():
        if label in conditions and local_at.weekday() != weekday:
            return False
    if conditions & {"weekday", "weekdays"} and local_at.weekday() >= 5:
        return False
    if conditions & {"weekend", "weekends"} and local_at.weekday() < 5:
        return False
    for label, (start, end) in _MEAL_PERIODS.items():
        if label in conditions and not start <= local_at.time() < end:
            return False
    return True


def _example(meal: MealEntry, revision: MealRevision) -> PatternEvidenceExample:
    return PatternEvidenceExample(
        evidence=QuestionEvidenceReference(kind="meal_revision", id=revision.id),
        occurred_at=meal.occurred_at or meal.created_at,
        occurred_utc_offset_minutes=meal.occurred_utc_offset_minutes,
        summary=(
            f"{meal.title} at {_local_occurrence(meal).isoformat()} "
            f"(meal revision {revision.number})."
        )[:500],
    )


class PatternDetectionService:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    async def detect_and_propose(
        self,
        *,
        account_id: str,
        max_proposals: int = 2,
    ) -> list[ClarificationQuestion]:
        if not 1 <= max_proposals <= 5:
            raise ValueError("pattern proposal limit must be between 1 and 5")
        evidence = await self._repository.recent_meal_evidence_for_account(
            account_id=account_id,
            limit=PATTERN_HISTORY_LIMIT,
        )
        eligible = [
            item
            for item in evidence
            if item[0].occurred_utc_offset_minutes is not None
            and item[0].confidence != Confidence.UNCERTAIN
            and item[0].status not in {MealStatus.CONTRADICTED, MealStatus.NOT_COOKING}
        ]
        candidates = self._candidate_cohorts(eligible)
        proposed: list[ClarificationQuestion] = []
        seen_topics: set[tuple[str, tuple[str, ...]]] = set()
        for candidate in candidates:
            topic = (candidate.claim.value, candidate.claim.conditions)
            if topic in seen_topics:
                continue
            seen_topics.add(topic)
            question = await self.propose(account_id=account_id, candidate=candidate)
            if question.status != QuestionStatus.OPEN:
                continue
            proposed.append(question)
            if len(proposed) == max_proposals:
                break
        return proposed

    @staticmethod
    def _candidate_cohorts(
        evidence: list[tuple[MealEntry, MealRevision]],
    ) -> list[PatternCandidate]:
        cohorts: dict[tuple[str, ...], list[tuple[MealEntry, MealRevision]]] = {}
        for item in evidence:
            meal, _ = item
            local_at = _local_occurrence(meal)
            weekday = local_at.strftime("%A").casefold()
            cohorts.setdefault((weekday,), []).append(item)
            if time(4) <= local_at.time() < time(11):
                day_group = "weekday" if local_at.weekday() < 5 else "weekend"
                cohorts.setdefault((day_group, "breakfast"), []).append(item)

        ranked: list[tuple[float, int, PatternCandidate]] = []
        for conditions, unbounded_cohort in cohorts.items():
            cohort = unbounded_cohort[:MAXIMUM_PATTERN_EVIDENCE]
            if len(cohort) < MINIMUM_SUPPORTING_EXAMPLES:
                continue
            labels: dict[str, list[tuple[MealEntry, MealRevision]]] = {}
            display_labels: dict[str, str] = {}
            for item in cohort:
                normalized = " ".join(item[0].title.casefold().split())
                labels.setdefault(normalized, []).append(item)
                display_labels.setdefault(normalized, " ".join(item[0].title.split()))
            ordered = sorted(
                labels,
                key=lambda label: (-len(labels[label]), label),
            )
            label_sets = [(ordered[0],)] if ordered else []
            if len(ordered) >= 2:
                label_sets.append((ordered[0], ordered[1]))
            for selected_labels in label_sets:
                support = [item for label in selected_labels for item in labels[label]]
                counters = [
                    item
                    for label, items in labels.items()
                    if label not in selected_labels
                    for item in items
                ]
                ratio = len(support) / len(cohort)
                if len(support) < MINIMUM_SUPPORTING_EXAMPLES or ratio < MINIMUM_SUPPORT_RATIO:
                    continue
                local_support_times = [_local_occurrence(meal) for meal, _ in support]
                if (
                    len(
                        {
                            (value.isocalendar().year, value.isocalendar().week)
                            for value in local_support_times
                        }
                    )
                    < MINIMUM_DISTINCT_WEEKS
                ):
                    continue
                if max(local_support_times) - min(local_support_times) < MINIMUM_OBSERVATION_SPAN:
                    continue
                display_value = " or ".join(display_labels[label] for label in selected_labels)
                value_label_tokens = _value_label_token_sets(display_value)
                if not value_label_tokens or any(
                    any(label_tokens <= _meal_tokens(meal) for label_tokens in value_label_tokens)
                    for meal, _ in counters
                ):
                    continue
                if len(conditions) == 1:
                    statement = f"you usually eat {display_value} on {conditions[0].title()}s"
                else:
                    statement = f"your {conditions[0]} {conditions[1]} is usually {display_value}"
                candidate = PatternCandidate(
                    statement=statement,
                    claim=KnowledgeClaim(
                        dimension="likely meal",
                        value=display_value,
                        conditions=conditions,
                    ),
                    supporting_revision_ids=tuple(revision.id for _, revision in support),
                    counterexample_revision_ids=tuple(revision.id for _, revision in counters),
                    uncertainty=(
                        "This is a calendar correlation from the retained journal, not a "
                        "confirmed household rule; changed plans and missing events may be "
                        "counterexamples."
                    ),
                )
                ranked.append((ratio, len(support), candidate))
                break
        return [
            candidate
            for _, _, candidate in sorted(
                ranked,
                key=lambda item: (-item[0], -item[1], item[2].statement),
            )
        ]

    async def propose(
        self,
        *,
        account_id: str,
        candidate: PatternCandidate,
    ) -> ClarificationQuestion:
        evidence = await self._repository.recent_meal_evidence_for_account(
            account_id=account_id,
            limit=PATTERN_HISTORY_LIMIT,
        )
        by_revision_id = {revision.id: (meal, revision) for meal, revision in evidence}
        requested_ids = {
            *candidate.supporting_revision_ids,
            *candidate.counterexample_revision_ids,
        }
        if not requested_ids <= set(by_revision_id):
            raise ValueError("pattern candidate cites unavailable meal revision evidence")
        supports = [
            by_revision_id[revision_id] for revision_id in candidate.supporting_revision_ids
        ]
        counters = [
            by_revision_id[revision_id] for revision_id in candidate.counterexample_revision_ids
        ]
        if len(supports) < MINIMUM_SUPPORTING_EXAMPLES:
            raise ValueError("pattern candidate has too few supporting examples")

        local_support_times = [_local_occurrence(meal) for meal, _ in supports]
        local_all_times = [_local_occurrence(meal) for meal, _ in (*supports, *counters)]
        support_weeks = {
            (value.isocalendar().year, value.isocalendar().week) for value in local_support_times
        }
        if len(support_weeks) < MINIMUM_DISTINCT_WEEKS:
            raise ValueError("pattern support must span at least three distinct weeks")
        if max(local_all_times) - min(local_all_times) < MINIMUM_OBSERVATION_SPAN:
            raise ValueError("pattern observation window is too short")
        if len(supports) / (len(supports) + len(counters)) < MINIMUM_SUPPORT_RATIO:
            raise ValueError("pattern has too many counterexamples")

        temporal_conditions = _recognized_temporal_conditions(candidate.claim)
        if not temporal_conditions:
            raise ValueError("longitudinal patterns require a recognized temporal condition")
        if not all(
            _matches_temporal_conditions(local_at, temporal_conditions)
            for local_at in local_all_times
        ):
            raise ValueError("cited evidence falls outside the claimed temporal cohort")

        value_label_tokens = _value_label_token_sets(candidate.claim.value)
        if not value_label_tokens:
            raise ValueError("pattern claim value has no testable meal label")
        if not all(
            any(label_tokens <= _meal_tokens(meal) for label_tokens in value_label_tokens)
            for meal, _ in supports
        ):
            raise ValueError("supporting meals do not visibly match the claim value")
        if any(
            any(label_tokens <= _meal_tokens(meal) for label_tokens in value_label_tokens)
            for meal, _ in counters
        ):
            raise ValueError("counterexamples visibly match the claim value")
        statement_tokens = _tokens(candidate.statement)
        if not any(label_tokens <= statement_tokens for label_tokens in value_label_tokens):
            raise ValueError("pattern statement does not name its claimed meal value")

        supporting_examples = [_example(meal, revision) for meal, revision in supports]
        counterexamples = [_example(meal, revision) for meal, revision in counters]
        observation_started_at = min(
            item.occurred_at for item in (*supporting_examples, *counterexamples)
        )
        observation_ended_at = max(
            item.occurred_at for item in (*supporting_examples, *counterexamples)
        )
        ratio = len(supports) / (len(supports) + len(counters))
        reason = (
            f"{len(supports)} of {len(supports) + len(counters)} cited meals "
            f"({ratio:.0%}) support this tentative pattern across "
            f"{len(set(value.date() for value in local_support_times))} dates and "
            f"{len(support_weeks)} weeks. "
            f"Uncertainty: {candidate.uncertainty}"
        )
        return await self._repository.open_pattern_question(
            account_id=account_id,
            prompt=f"I'm noticing {candidate.statement}. Is that accurate?",
            reason=reason,
            tentative_claim=candidate.statement,
            evidence=[item.evidence for item in (*supporting_examples, *counterexamples)],
            pattern_claim=candidate.claim,
            observation_started_at=observation_started_at,
            observation_ended_at=observation_ended_at,
            supporting_examples=supporting_examples,
            counterexamples=counterexamples,
            prompt_version=PATTERN_DETECTION_VERSION,
            uncertainty=candidate.uncertainty,
        )
