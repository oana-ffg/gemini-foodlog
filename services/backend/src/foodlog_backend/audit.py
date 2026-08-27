from hashlib import sha256
from typing import Protocol

from .models import AuditAction, AuditActorKind, AuditEvent, AuditPurpose, AuditSource


class AuditRepository(Protocol):
    async def append_audit_event(self, event: AuditEvent) -> AuditEvent: ...


def build_audit_event(
    *,
    account_id: str,
    action: AuditAction,
    actor_kind: AuditActorKind,
    source: AuditSource,
    subject_kind: str,
    subject_id: str,
    purpose: AuditPurpose | None = None,
    occurrence_id: str | None = None,
) -> AuditEvent:
    identity_fields = [
        "foodlog-audit-v1",
        account_id,
        action.value,
        actor_kind.value,
        source.value,
        subject_kind,
        subject_id,
    ]
    if purpose is not None or occurrence_id is not None:
        identity_fields.extend((purpose.value if purpose is not None else "", occurrence_id or ""))
    identity = "\0".join(identity_fields)
    return AuditEvent(
        id=sha256(identity.encode()).hexdigest(),
        account_id=account_id,
        action=action,
        actor_kind=actor_kind,
        source=source,
        subject_kind=subject_kind,
        subject_id=subject_id,
        purpose=purpose,
    )


async def record_audit_event(
    repository: AuditRepository,
    *,
    account_id: str,
    action: AuditAction,
    actor_kind: AuditActorKind,
    source: AuditSource,
    subject_kind: str,
    subject_id: str,
    purpose: AuditPurpose | None = None,
    occurrence_id: str | None = None,
) -> AuditEvent:
    return await repository.append_audit_event(
        build_audit_event(
            account_id=account_id,
            action=action,
            actor_kind=actor_kind,
            source=source,
            subject_kind=subject_kind,
            subject_id=subject_id,
            purpose=purpose,
            occurrence_id=occurrence_id,
        )
    )
