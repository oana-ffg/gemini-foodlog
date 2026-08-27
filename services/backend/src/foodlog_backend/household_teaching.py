from __future__ import annotations

from hashlib import sha256
from unicodedata import normalize as unicode_normalize

from .errors import KnowledgeRevisionConflict
from .models import (
    KnowledgeBeliefStrength,
    KnowledgeEvidenceKind,
    KnowledgeEvidenceReference,
    KnowledgeEvidenceRole,
    KnowledgeLifecycle,
    KnowledgePageHistory,
    KnowledgeRevision,
    KnowledgeRevisionDraft,
    KnowledgeRevisionResult,
    KnowledgeRevisionSource,
    StableKnowledgeCorrectionCreate,
    StableKnowledgeRetirementCreate,
    StableKnowledgeTeachingCreate,
    StableKnowledgeTeachingResult,
    UserContextNote,
    UserContextNoteCreate,
)
from .repository import Repository


def stable_statement_topic_key(statement: str) -> str:
    normalized = " ".join(unicode_normalize("NFKC", statement).casefold().split())
    return f"explicit-user-statement:{sha256(normalized.encode()).hexdigest()}"


def stable_statement_title(statement: str) -> str:
    compact = " ".join(statement.split())
    return compact[:200]


class HouseholdTeachingService:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    async def teach(
        self,
        *,
        owner_user_id: str,
        request: StableKnowledgeTeachingCreate,
        idempotency_key: str,
    ) -> StableKnowledgeTeachingResult:
        account = await self._repository.account_for_owner(owner_user_id)
        note_key = f"stable-teaching-note-v1:{idempotency_key}"
        revision_key = f"stable-teaching-revision-v1:{idempotency_key}"
        note = await self._repository.create_user_context_note(
            owner_user_id=owner_user_id,
            request=UserContextNoteCreate(text=request.statement),
            idempotency_key=note_key,
        )
        try:
            existing = await self._repository.knowledge_revision_result_for_request(
                account_id=account.id,
                idempotency_key=revision_key,
            )
            if existing is None:
                topic_key = stable_statement_topic_key(request.statement)
                current = await self._repository.current_knowledge_revision(
                    account_id=account.id,
                    topic_key=topic_key,
                )
                result = await self._repository.record_knowledge_revision(
                    account_id=account.id,
                    topic_key=topic_key,
                    expected_revision_number=(
                        current.revision.number if current is not None else None
                    ),
                    draft=self._confirmed_statement_draft(
                        statement=request.statement,
                        source_note=note,
                        predecessor=(current.revision if current is not None else None),
                        reason=(
                            "The authenticated user explicitly supplied this standalone "
                            "household statement."
                        ),
                    ),
                    idempotency_key=revision_key,
                )
            else:
                result = existing
        finally:
            note = await self._repository.retire_user_context_note(
                owner_user_id=owner_user_id,
                note_id=note.id,
            )
        return StableKnowledgeTeachingResult(
            source_note=note,
            page=result.page,
            revision=result.revision,
        )

    async def correct(
        self,
        *,
        owner_user_id: str,
        page_id: str,
        request: StableKnowledgeCorrectionCreate,
        idempotency_key: str,
    ) -> StableKnowledgeTeachingResult:
        account = await self._repository.account_for_owner(owner_user_id)
        note_key = f"stable-correction-note-v1:{page_id}:{idempotency_key}"
        revision_key = f"stable-correction-revision-v1:{page_id}:{idempotency_key}"
        note = await self._repository.create_user_context_note(
            owner_user_id=owner_user_id,
            request=UserContextNoteCreate(text=request.statement),
            idempotency_key=note_key,
        )
        try:
            existing = await self._repository.knowledge_revision_result_for_request(
                account_id=account.id,
                idempotency_key=revision_key,
            )
            if existing is None:
                page = await self._repository.knowledge_page_for_owner(
                    owner_user_id,
                    page_id,
                )
                current = await self._repository.current_knowledge_revision(
                    account_id=account.id,
                    topic_key=page.topic_key,
                )
                if (
                    current is None
                    or current.page.id != page_id
                    or current.revision.number != request.expected_revision_number
                ):
                    raise KnowledgeRevisionConflict
                result = await self._repository.record_knowledge_revision(
                    account_id=account.id,
                    topic_key=page.topic_key,
                    expected_revision_number=request.expected_revision_number,
                    draft=self._confirmed_statement_draft(
                        statement=request.statement,
                        source_note=note,
                        predecessor=current.revision,
                        reason=(
                            "The authenticated user explicitly corrected this standalone "
                            "household statement."
                        ),
                    ),
                    idempotency_key=revision_key,
                )
            else:
                result = existing
        finally:
            note = await self._repository.retire_user_context_note(
                owner_user_id=owner_user_id,
                note_id=note.id,
            )
        return StableKnowledgeTeachingResult(
            source_note=note,
            page=result.page,
            revision=result.revision,
        )

    async def retire(
        self,
        *,
        owner_user_id: str,
        page_id: str,
        request: StableKnowledgeRetirementCreate,
        idempotency_key: str,
    ) -> KnowledgeRevisionResult:
        account = await self._repository.account_for_owner(owner_user_id)
        revision_key = f"stable-retirement-revision-v1:{page_id}:{idempotency_key}"
        existing = await self._repository.knowledge_revision_result_for_request(
            account_id=account.id,
            idempotency_key=revision_key,
        )
        if existing is not None:
            return existing
        page = await self._repository.knowledge_page_for_owner(owner_user_id, page_id)
        current = await self._repository.current_knowledge_revision(
            account_id=account.id,
            topic_key=page.topic_key,
        )
        if (
            current is None
            or current.page.id != page_id
            or current.revision.number != request.expected_revision_number
        ):
            raise KnowledgeRevisionConflict
        return await self._repository.record_knowledge_revision(
            account_id=account.id,
            topic_key=page.topic_key,
            expected_revision_number=request.expected_revision_number,
            draft=KnowledgeRevisionDraft(
                title=current.revision.title,
                statement=current.revision.statement,
                claim=current.revision.claim,
                lifecycle=KnowledgeLifecycle.RETIRED,
                belief_strength=KnowledgeBeliefStrength.WEAK,
                source=KnowledgeRevisionSource.USER_STATEMENT,
                evidence=[
                    KnowledgeEvidenceReference(
                        kind=KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                        id=current.revision.id,
                        role=KnowledgeEvidenceRole.CONTEXT,
                    )
                ],
                reason=(
                    request.reason
                    or "The authenticated user explicitly retired this household knowledge."
                ),
            ),
            idempotency_key=revision_key,
        )

    async def page_history(
        self,
        *,
        owner_user_id: str,
        page_id: str,
    ) -> KnowledgePageHistory:
        page = await self._repository.knowledge_page_for_owner(owner_user_id, page_id)
        revisions = await self._repository.list_knowledge_revisions(
            owner_user_id,
            page_id,
        )
        return KnowledgePageHistory(page=page, revisions=revisions)

    @staticmethod
    def _confirmed_statement_draft(
        *,
        statement: str,
        source_note: UserContextNote,
        predecessor: KnowledgeRevision | None,
        reason: str,
    ) -> KnowledgeRevisionDraft:
        evidence = [
            KnowledgeEvidenceReference(
                kind=KnowledgeEvidenceKind.USER_CONTEXT_NOTE,
                id=source_note.id,
                role=KnowledgeEvidenceRole.SUPPORTS,
                note="Exact raw standalone statement supplied by the authenticated user.",
            )
        ]
        if predecessor is not None:
            evidence.append(
                KnowledgeEvidenceReference(
                    kind=KnowledgeEvidenceKind.KNOWLEDGE_REVISION,
                    id=predecessor.id,
                    role=KnowledgeEvidenceRole.CONTEXT,
                )
            )
        return KnowledgeRevisionDraft(
            title=stable_statement_title(statement),
            statement=statement,
            claim=None,
            lifecycle=KnowledgeLifecycle.CONFIRMED,
            belief_strength=KnowledgeBeliefStrength.STRONG,
            source=KnowledgeRevisionSource.USER_STATEMENT,
            evidence=evidence,
            reason=reason,
        )
