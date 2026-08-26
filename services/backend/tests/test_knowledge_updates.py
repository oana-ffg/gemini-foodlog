import asyncio

import pytest

from foodlog_backend.knowledge_updates import (
    ConfirmedClaimSource,
    HouseholdKnowledgeUpdater,
    KnowledgeUpdateIntent,
    KnowledgeUpdateProposal,
)
from foodlog_backend.models import (
    KnowledgeClaim,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeLifecycle,
    KnowledgeRevisionSource,
)
from foodlog_backend.repository import InMemoryRepository


def evidence(
    kind: KnowledgeEvidenceKind,
    identity: str,
    role: KnowledgeEvidenceRole,
) -> KnowledgeEvidenceReference:
    return KnowledgeEvidenceReference(kind=kind, id=identity, role=role)


def test_weak_inference_is_applied_without_borrowing_confirmation() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("weak-inference-owner")
        updater = HouseholdKnowledgeUpdater(repository)
        proposal = KnowledgeUpdateProposal(
            topic_key="air fryer red meat",
            title="Air-fryer red meat",
            statement="Steak may be the usual red meat prepared in the air fryer.",
            claim=KnowledgeClaim(
                dimension="likely meal",
                value="steak",
                conditions=("air fryer", "red meat visible"),
            ),
            intent=KnowledgeUpdateIntent.INFER,
            source=KnowledgeRevisionSource.AGENT_INFERENCE,
            evidence=(
                evidence(
                    KnowledgeEvidenceKind.MEAL_REVISION,
                    "meal-revision-weak-001",
                    KnowledgeEvidenceRole.SUPPORTS,
                ),
            ),
            reason="One corrected event weakly suggests a reusable distinction.",
        )

        applied = await updater.apply(
            account_id=account.id,
            proposal=proposal,
            expected_revision_number=None,
            idempotency_key="weak-inference-0001",
        )
        retry = await updater.apply(
            account_id=account.id,
            proposal=proposal,
            expected_revision_number=None,
            idempotency_key="weak-inference-0001",
        )

        assert retry == applied
        assert applied.revision.lifecycle == KnowledgeLifecycle.INFERRED
        assert applied.revision.claim == proposal.claim
        assert applied.revision.number == 1

    asyncio.run(scenario())


def test_exact_user_scope_can_create_confirmed_knowledge_but_broadening_cannot() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("confirmed-scope-owner")
        updater = HouseholdKnowledgeUpdater(repository)
        feedback = evidence(
            KnowledgeEvidenceKind.FEEDBACK,
            "feedback-scope-001",
            KnowledgeEvidenceRole.SUPPORTS,
        )
        user_claim = KnowledgeClaim(
            dimension="likely meal",
            value="steak",
            conditions=("air fryer", "thursday"),
        )
        exact = KnowledgeUpdateProposal(
            topic_key="thursday air fryer meal",
            title="Thursday air-fryer meal",
            statement="On Thursdays, air-fryer cooking is usually steak.",
            claim=user_claim,
            intent=KnowledgeUpdateIntent.CONFIRM,
            source=KnowledgeRevisionSource.USER_FEEDBACK,
            evidence=(feedback,),
            confirmed_sources=(ConfirmedClaimSource(claim=user_claim, evidence=feedback),),
            reason="The user explicitly supplied this exact reusable distinction.",
        )
        broader_claim = KnowledgeClaim(
            dimension="likely meal",
            value="steak",
            conditions=("air fryer",),
        )
        broader = exact.model_copy(
            update={
                "topic_key": "all air fryer meals",
                "title": "All air-fryer meals",
                "statement": "Air-fryer cooking is usually steak.",
                "claim": broader_claim,
            }
        )

        confirmed = await updater.apply(
            account_id=account.id,
            proposal=exact,
            expected_revision_number=None,
            idempotency_key="confirmed-scope-0001",
        )
        inferred = await updater.apply(
            account_id=account.id,
            proposal=broader,
            expected_revision_number=None,
            idempotency_key="broader-scope-0001",
        )

        assert confirmed.revision.lifecycle == KnowledgeLifecycle.CONFIRMED
        assert inferred.revision.lifecycle == KnowledgeLifecycle.INFERRED

    asyncio.run(scenario())


def test_user_correction_can_contradict_but_never_erase_prior_revision() -> None:
    async def scenario() -> None:
        repository = InMemoryRepository(public_account_limit=25, trial_image_limit=200)
        account = await repository.provision_account("contradiction-owner")
        updater = HouseholdKnowledgeUpdater(repository)
        confirmation = evidence(
            KnowledgeEvidenceKind.FEEDBACK,
            "feedback-confirm-001",
            KnowledgeEvidenceRole.SUPPORTS,
        )
        original_claim = KnowledgeClaim(
            dimension="likely meal",
            value="steak",
            conditions=("air fryer", "red meat visible"),
        )
        original = await updater.apply(
            account_id=account.id,
            proposal=KnowledgeUpdateProposal(
                topic_key="air fryer red meat",
                title="Air-fryer red meat",
                statement="Visible red meat in the air fryer is steak.",
                claim=original_claim,
                intent=KnowledgeUpdateIntent.CONFIRM,
                source=KnowledgeRevisionSource.USER_FEEDBACK,
                evidence=(confirmation,),
                confirmed_sources=(
                    ConfirmedClaimSource(claim=original_claim, evidence=confirmation),
                ),
                reason="The user confirmed the exact distinction.",
            ),
            expected_revision_number=None,
            idempotency_key="contradiction-original-0001",
        )
        correction = evidence(
            KnowledgeEvidenceKind.FEEDBACK,
            "feedback-contradict-001",
            KnowledgeEvidenceRole.CONTRADICTS,
        )
        contradicted = await updater.apply(
            account_id=account.id,
            proposal=KnowledgeUpdateProposal(
                topic_key="air fryer red meat",
                title="Air-fryer red meat",
                statement="Visible red meat in the air fryer is not always steak.",
                claim=KnowledgeClaim(
                    dimension="likely meal",
                    value="not always steak",
                    conditions=("air fryer", "red meat visible"),
                ),
                intent=KnowledgeUpdateIntent.CONTRADICT,
                source=KnowledgeRevisionSource.USER_FEEDBACK,
                evidence=(
                    evidence(
                        KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                        original.revision.id,
                        KnowledgeEvidenceRole.CONTEXT,
                    ),
                    correction,
                ),
                reason="A later explicit correction contradicts the old absolute rule.",
            ),
            expected_revision_number=1,
            idempotency_key="contradiction-update-0001",
        )

        revisions = await repository.list_knowledge_revisions(
            "contradiction-owner",
            original.page.id,
        )
        assert contradicted.revision.lifecycle == KnowledgeLifecycle.CONTRADICTED
        assert [item.lifecycle for item in revisions] == [
            KnowledgeLifecycle.CONFIRMED,
            KnowledgeLifecycle.CONTRADICTED,
        ]
        assert revisions[0].statement == "Visible red meat in the air fryer is steak."

    asyncio.run(scenario())


def test_confirmation_rejects_unlinked_scope_evidence() -> None:
    feedback = evidence(
        KnowledgeEvidenceKind.FEEDBACK,
        "feedback-validation-001",
        KnowledgeEvidenceRole.SUPPORTS,
    )
    claim = KnowledgeClaim(dimension="likely meal", value="steak")

    with pytest.raises(ValueError, match="must link proposal evidence"):
        KnowledgeUpdateProposal(
            topic_key="invalid confirmation",
            title="Invalid confirmation",
            statement="Steak is usual.",
            claim=claim,
            intent=KnowledgeUpdateIntent.CONFIRM,
            source=KnowledgeRevisionSource.USER_FEEDBACK,
            evidence=(feedback,),
            confirmed_sources=(
                ConfirmedClaimSource(
                    claim=claim,
                    evidence=feedback.model_copy(update={"id": "different-feedback"}),
                ),
            ),
            reason="This scope is not linked to the proposal evidence.",
        )
