from __future__ import annotations

import asyncio
from collections.abc import Callable
from hashlib import sha256
from typing import Protocol

import dkim
import dns.exception
import dns.rdatatype
import dns.resolver

from .models import (
    RawMailAuthentication,
    RawMailAuthenticationOutcome,
)

DKIM_VERIFIER_VERSION = "aligned-dkim-v1"
MAX_DKIM_SIGNATURES = 10
MAX_ALIGNED_SIGNATURE_ATTEMPTS = 3
DKIM_DNS_TIMEOUT_SECONDS = 3
TRUSTED_SIGNER_DOMAIN = "nemlig.com"
_ALLOWED_ALGORITHMS = frozenset({b"rsa-sha256"})
_ALWAYS_REQUIRED_SIGNED_HEADERS = frozenset({b"from", b"subject"})
_CONDITIONAL_SIGNED_HEADERS = frozenset(
    {b"mime-version", b"content-type", b"content-transfer-encoding"}
)

DnsTxtLookup = Callable[..., bytes | None]


def _resolve_txt(name: bytes, *, timeout: int) -> bytes | None:
    try:
        hostname = name.decode("ascii")
    except UnicodeDecodeError:
        return None
    try:
        answer = dns.resolver.resolve(
            hostname,
            dns.rdatatype.TXT,
            raise_on_no_answer=False,
            lifetime=timeout,
            search=False,
        )
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return None
    except (
        dns.resolver.NoNameservers,
        dns.resolver.NoResolverConfiguration,
        dns.exception.Timeout,
    ) as error:
        raise _DnsOperationalError from error
    for record_set in answer.response.answer:
        if record_set.rdtype == dns.rdatatype.TXT and record_set.items:
            return b"".join(next(iter(record_set.items)).strings)
    return None


class MailAuthenticationTemporaryFailure(RuntimeError):
    """A retryable verifier or DNS failure, distinct from an untrusted message."""


class _DnsOperationalError(RuntimeError):
    pass


class MailAuthenticator(Protocol):
    async def authenticate(
        self,
        raw_message: bytes,
        *,
        account_id: str,
        raw_mail_id: str,
    ) -> RawMailAuthentication: ...


def _domain_is_aligned(domain: bytes) -> bool:
    try:
        normalized = domain.decode("ascii").strip().casefold().rstrip(".")
    except UnicodeDecodeError:
        return False
    return normalized == TRUSTED_SIGNER_DOMAIN or normalized.endswith(
        f".{TRUSTED_SIGNER_DOMAIN}"
    )


def _untrusted_authentication(
    *,
    account_id: str,
    raw_mail_id: str,
    raw_content_sha256: str,
) -> RawMailAuthentication:
    return RawMailAuthentication(
        id=raw_mail_id,
        account_id=account_id,
        raw_mail_id=raw_mail_id,
        raw_content_sha256=raw_content_sha256,
        outcome=RawMailAuthenticationOutcome.UNTRUSTED,
        verifier_version=DKIM_VERIFIER_VERSION,
    )


class DkimMailAuthenticator:
    """Verify bounded, aligned DKIM evidence from the exact retained RFC message."""

    def __init__(self, *, dnsfunc: DnsTxtLookup = _resolve_txt) -> None:
        self._dnsfunc = dnsfunc

    def _lookup_txt(self, name: bytes, *, timeout: int) -> bytes | None:
        try:
            return self._dnsfunc(name, timeout=timeout)
        except (
            _DnsOperationalError,
            dkim.DnsTimeoutError,
            dns.resolver.NoNameservers,
            dns.resolver.NoResolverConfiguration,
            dns.exception.Timeout,
            TimeoutError,
            OSError,
        ) as error:
            # dkimpy converts DnsTimeoutError into a normal signature failure. Raise a
            # distinct type so transient resolver failures remain retryable.
            raise _DnsOperationalError from error

    async def authenticate(
        self,
        raw_message: bytes,
        *,
        account_id: str,
        raw_mail_id: str,
    ) -> RawMailAuthentication:
        return await asyncio.to_thread(
            self._authenticate_sync,
            raw_message,
            account_id=account_id,
            raw_mail_id=raw_mail_id,
        )

    def _authenticate_sync(
        self,
        raw_message: bytes,
        *,
        account_id: str,
        raw_mail_id: str,
    ) -> RawMailAuthentication:
        raw_content_sha256 = sha256(raw_message).hexdigest()
        try:
            verifier = dkim.DKIM(
                raw_message,
                minkey=1024,
                timeout=DKIM_DNS_TIMEOUT_SECONDS,
            )
        except (dkim.DKIMException, ValueError, IndexError):
            return _untrusted_authentication(
                account_id=account_id,
                raw_mail_id=raw_mail_id,
                raw_content_sha256=raw_content_sha256,
            )

        singleton_headers = {
            name
            for name in _ALWAYS_REQUIRED_SIGNED_HEADERS | _CONDITIONAL_SIGNED_HEADERS
            if sum(1 for header_name, _ in verifier.headers if header_name.lower() == name) > 1
        }
        signatures = [
            value
            for name, value in verifier.headers
            if name.lower() == b"dkim-signature"
        ]
        if singleton_headers or not signatures or len(signatures) > MAX_DKIM_SIGNATURES:
            return _untrusted_authentication(
                account_id=account_id,
                raw_mail_id=raw_mail_id,
                raw_content_sha256=raw_content_sha256,
            )

        present_conditional_headers = {
            name
            for name in _CONDITIONAL_SIGNED_HEADERS
            if any(header_name.lower() == name for header_name, _ in verifier.headers)
        }
        required_signed_headers = (
            _ALWAYS_REQUIRED_SIGNED_HEADERS | present_conditional_headers
        )
        aligned_attempts = 0
        for index, signature in enumerate(signatures):
            try:
                tags = dkim.parse_tag_value(signature)
            except (dkim.DKIMException, ValueError):
                continue
            domain = tags.get(b"d", b"")
            if not _domain_is_aligned(domain):
                continue
            aligned_attempts += 1
            if aligned_attempts > MAX_ALIGNED_SIGNATURE_ATTEMPTS:
                break
            signed_headers = tuple(
                header.strip().lower()
                for header in tags.get(b"h", b"").split(b":")
                if header.strip()
            )
            if (
                b"l" in tags
                or tags.get(b"a", b"").lower() not in _ALLOWED_ALGORITHMS
                or not required_signed_headers.issubset(signed_headers)
            ):
                continue
            try:
                verified = verifier.verify(idx=index, dnsfunc=self._lookup_txt)
            except _DnsOperationalError as error:
                raise MailAuthenticationTemporaryFailure("dkim_dns_failure") from error
            except (TimeoutError, OSError) as error:
                raise MailAuthenticationTemporaryFailure("dkim_dns_failure") from error
            except (dkim.DKIMException, ValueError, IndexError):
                continue
            except Exception as error:
                raise MailAuthenticationTemporaryFailure("dkim_verifier_failure") from error
            if verified:
                return RawMailAuthentication(
                    id=raw_mail_id,
                    account_id=account_id,
                    raw_mail_id=raw_mail_id,
                    raw_content_sha256=raw_content_sha256,
                    outcome=RawMailAuthenticationOutcome.ALIGNED_DKIM_PASS,
                    signer_domain=domain.decode("ascii").casefold().rstrip("."),
                    signed_headers=tuple(
                        sorted({header.decode("ascii") for header in signed_headers})
                    ),
                    verifier_version=DKIM_VERIFIER_VERSION,
                )

        return _untrusted_authentication(
            account_id=account_id,
            raw_mail_id=raw_mail_id,
            raw_content_sha256=raw_content_sha256,
        )
