from foodlog_backend.models import (
    PurchaseDocumentCandidate,
    RawMailAuthentication,
    RawMailAuthenticationOutcome,
)


def trusted_authentication_for_candidate(
    candidate: PurchaseDocumentCandidate,
) -> RawMailAuthentication:
    return RawMailAuthentication(
        id=candidate.raw_mail_id,
        account_id=candidate.account_id,
        raw_mail_id=candidate.raw_mail_id,
        raw_content_sha256=candidate.raw_content_sha256,
        outcome=RawMailAuthenticationOutcome.ALIGNED_DKIM_PASS,
        signer_domain="nemlig.com",
        signed_headers=("from", "subject", "mime-version", "content-type"),
        verifier_version="test-aligned-dkim-v1",
    )


async def seed_authenticated_raw_mail(repository, candidate: PurchaseDocumentCandidate) -> None:
    await repository.seed_published_raw_mail(
        account_id=candidate.account_id,
        raw_mail_id=candidate.raw_mail_id,
        content_sha256=candidate.raw_content_sha256,
    )
    await repository.record_raw_mail_authentication(
        trusted_authentication_for_candidate(candidate)
    )
