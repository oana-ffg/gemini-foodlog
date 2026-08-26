import asyncio

import pytest

from foodlog_backend.errors import (
    IdempotencyConflict,
    InvalidKnowledgeProvenance,
    InvalidKnowledgeTransition,
    KnowledgePageNotFound,
    KnowledgeRevisionConflict,
)
from foodlog_backend.models import (
    KnowledgeBeliefStrength,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeLifecycle,
    KnowledgeRevisionDraft,
    KnowledgeRevisionSource,
)
from foodlog_backend.repository import InMemoryRepository


def evidence(
    kind: KnowledgeEvidenceKind,
    identity: str,
    role: KnowledgeEvidenceRole,
) -> KnowledgeEvidenceReference:
    return KnowledgeEvidenceReference(kind=kind, id=identity, role=role)


def draft(
    *,
    lifecycle: KnowledgeLifecycle,
    strength: KnowledgeBeliefStrength,
    statement: str,
    evidence_refs: list[KnowledgeEvidenceReference],
    source: KnowledgeRevisionSource = KnowledgeRevisionSource.AGENT_INFERENCE,
) -> KnowledgeRevisionDraft:
    return KnowledgeRevisionDraft(
        title="Air-fryer meat preference",
        statement=statement,
        lifecycle=lifecycle,
        belief_strength=strength,
        source=source,
        evidence=evidence_refs,
        reason=f"Record the {lifecycle.value} household belief with exact provenance.",
    )


def test_household_knowledge_lifecycle_is_immutable_idempotent_and_tenant_scoped() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        owner = await repository.provision_account("knowledge-owner")
        await repository.provision_account("knowledge-foreign-owner")
        initial_draft = draft(
            lifecycle=KnowledgeLifecycle.INFERRED,
            strength=KnowledgeBeliefStrength.WEAK,
            statement="Steak may be the usual red meat cooked in the air fryer.",
            evidence_refs=[
                evidence(
                    KnowledgeEvidenceKind.MEAL_REVISION,
                    "meal-revision-001",
                    KnowledgeEvidenceRole.SUPPORTS,
                )
            ],
        )
        initial = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key="  Air-Fryer   Meat Preference ",
            expected_revision_number=None,
            draft=initial_draft,
            idempotency_key="knowledge-inferred-0001",
        )
        retry = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key="air-fryer meat preference",
            expected_revision_number=None,
            draft=initial_draft,
            idempotency_key="knowledge-inferred-0001",
        )
        assert retry == initial
        assert initial.page.topic_key == "air-fryer meat preference"
        assert initial.revision.number == 1
        assert initial.revision.base_revision_number is None
        assert initial.revision.previous_revision_id is None

        with pytest.raises(IdempotencyConflict):
            await repository.record_knowledge_revision(
                account_id=owner.id,
                topic_key="air-fryer meat preference",
                expected_revision_number=None,
                draft=initial_draft.model_copy(update={"statement": "Changed retry"}),
                idempotency_key="knowledge-inferred-0001",
            )

        reinforced_draft = draft(
            lifecycle=KnowledgeLifecycle.REINFORCED,
            strength=KnowledgeBeliefStrength.MODERATE,
            statement="Steak is usually the red meat cooked in the air fryer.",
            evidence_refs=[
                evidence(
                    KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                    initial.revision.id,
                    KnowledgeEvidenceRole.CONTEXT,
                ),
                evidence(
                    KnowledgeEvidenceKind.MEAL_REVISION,
                    "meal-revision-002",
                    KnowledgeEvidenceRole.SUPPORTS,
                ),
            ],
        )
        reinforced = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key=initial.page.topic_key,
            expected_revision_number=1,
            draft=reinforced_draft,
            idempotency_key="knowledge-reinforced-0001",
        )
        assert reinforced.revision.number == 2
        assert reinforced.revision.base_revision_number == 1
        assert reinforced.revision.previous_revision_id == initial.revision.id

        with pytest.raises(KnowledgeRevisionConflict):
            await repository.record_knowledge_revision(
                account_id=owner.id,
                topic_key=initial.page.topic_key,
                expected_revision_number=1,
                draft=reinforced_draft,
                idempotency_key="knowledge-stale-0001",
            )
        with pytest.raises(InvalidKnowledgeProvenance):
            await repository.record_knowledge_revision(
                account_id=owner.id,
                topic_key=initial.page.topic_key,
                expected_revision_number=2,
                draft=draft(
                    lifecycle=KnowledgeLifecycle.CONFIRMED,
                    strength=KnowledgeBeliefStrength.STRONG,
                    statement="Steak is the usual red meat cooked in the air fryer.",
                    evidence_refs=[
                        evidence(
                            KnowledgeEvidenceKind.FEEDBACK,
                            "feedback-001",
                            KnowledgeEvidenceRole.SUPPORTS,
                        )
                    ],
                    source=KnowledgeRevisionSource.USER_FEEDBACK,
                ),
                idempotency_key="knowledge-missing-lineage-0001",
            )

        confirmed = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key=initial.page.topic_key,
            expected_revision_number=2,
            draft=draft(
                lifecycle=KnowledgeLifecycle.CONFIRMED,
                strength=KnowledgeBeliefStrength.STRONG,
                statement="Steak is the usual red meat cooked in the air fryer.",
                evidence_refs=[
                    evidence(
                        KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                        reinforced.revision.id,
                        KnowledgeEvidenceRole.CONTEXT,
                    ),
                    evidence(
                        KnowledgeEvidenceKind.QUESTION_RESPONSE,
                        "question-response-001",
                        KnowledgeEvidenceRole.SUPPORTS,
                    ),
                ],
                source=KnowledgeRevisionSource.QUESTION_RESPONSE,
            ),
            idempotency_key="knowledge-confirmed-0001",
        )
        contradicted = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key=initial.page.topic_key,
            expected_revision_number=3,
            draft=draft(
                lifecycle=KnowledgeLifecycle.CONTRADICTED,
                strength=KnowledgeBeliefStrength.WEAK,
                statement="Steak is not always the red meat cooked in the air fryer.",
                evidence_refs=[
                    evidence(
                        KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                        confirmed.revision.id,
                        KnowledgeEvidenceRole.CONTEXT,
                    ),
                    evidence(
                        KnowledgeEvidenceKind.FEEDBACK,
                        "feedback-contradiction-001",
                        KnowledgeEvidenceRole.CONTRADICTS,
                    ),
                ],
                source=KnowledgeRevisionSource.USER_FEEDBACK,
            ),
            idempotency_key="knowledge-contradicted-0001",
        )
        retired = await repository.record_knowledge_revision(
            account_id=owner.id,
            topic_key=initial.page.topic_key,
            expected_revision_number=4,
            draft=draft(
                lifecycle=KnowledgeLifecycle.RETIRED,
                strength=KnowledgeBeliefStrength.WEAK,
                statement="The old air-fryer meat belief is retired.",
                evidence_refs=[
                    evidence(
                        KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                        contradicted.revision.id,
                        KnowledgeEvidenceRole.CONTEXT,
                    ),
                    evidence(
                        KnowledgeEvidenceKind.FEEDBACK,
                        "feedback-retire-001",
                        KnowledgeEvidenceRole.SUPPORTS,
                    ),
                ],
                source=KnowledgeRevisionSource.USER_FEEDBACK,
            ),
            idempotency_key="knowledge-retired-0001",
        )

        current = await repository.knowledge_page_for_owner("knowledge-owner", retired.page.id)
        revisions = await repository.list_knowledge_revisions(
            "knowledge-owner", retired.page.id
        )
        assert current == retired.page
        assert current.current_revision_number == 5
        assert [revision.number for revision in revisions] == [1, 2, 3, 4, 5]
        assert [revision.lifecycle for revision in revisions] == [
            KnowledgeLifecycle.INFERRED,
            KnowledgeLifecycle.REINFORCED,
            KnowledgeLifecycle.CONFIRMED,
            KnowledgeLifecycle.CONTRADICTED,
            KnowledgeLifecycle.RETIRED,
        ]
        assert revisions[0].statement == initial_draft.statement

        with pytest.raises(InvalidKnowledgeTransition):
            await repository.record_knowledge_revision(
                account_id=owner.id,
                topic_key=initial.page.topic_key,
                expected_revision_number=5,
                draft=draft(
                    lifecycle=KnowledgeLifecycle.INFERRED,
                    strength=KnowledgeBeliefStrength.WEAK,
                    statement="A retired page cannot silently reactivate.",
                    evidence_refs=[
                        evidence(
                            KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                            retired.revision.id,
                            KnowledgeEvidenceRole.CONTEXT,
                        )
                    ],
                ),
                idempotency_key="knowledge-reactivate-0001",
            )
        with pytest.raises(KnowledgePageNotFound):
            await repository.knowledge_page_for_owner(
                "knowledge-foreign-owner", retired.page.id
            )
        with pytest.raises(KnowledgePageNotFound):
            await repository.list_knowledge_revisions(
                "knowledge-foreign-owner", retired.page.id
            )

    asyncio.run(scenario())


def test_knowledge_draft_requires_unique_and_explicit_contradicting_provenance() -> None:
    duplicate = evidence(
        KnowledgeEvidenceKind.MEAL_REVISION,
        "meal-revision-duplicate",
        KnowledgeEvidenceRole.SUPPORTS,
    )
    with pytest.raises(ValueError, match="must be unique"):
        draft(
            lifecycle=KnowledgeLifecycle.INFERRED,
            strength=KnowledgeBeliefStrength.WEAK,
            statement="A duplicated source is not valid provenance.",
            evidence_refs=[duplicate, duplicate],
        )
    with pytest.raises(ValueError, match="requires contradicting evidence"):
        draft(
            lifecycle=KnowledgeLifecycle.CONTRADICTED,
            strength=KnowledgeBeliefStrength.WEAK,
            statement="A contradiction needs explicit contradicting evidence.",
            evidence_refs=[duplicate],
        )
