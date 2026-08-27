from hashlib import sha256
from typing import Protocol

from .models import AuditAction, AuditActorKind, AuditEvent, AuditSource


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
) -> AuditEvent:
    identity = "\0".join(
        (
            "foodlog-audit-v1",
            account_id,
            action.value,
            actor_kind.value,
            source.value,
            subject_kind,
            subject_id,
        )
    )
    return AuditEvent(
        id=sha256(identity.encode()).hexdigest(),
        account_id=account_id,
        action=action,
        actor_kind=actor_kind,
        source=source,
        subject_kind=subject_kind,
        subject_id=subject_id,
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
) -> AuditEvent:
    return await repository.append_audit_event(
        build_audit_event(
            account_id=account_id,
            action=action,
            actor_kind=actor_kind,
            source=source,
            subject_kind=subject_kind,
            subject_id=subject_id,
        )
    )
