import asyncio
from base64 import b64encode
from email.message import EmailMessage
from email.policy import SMTP

import dkim
import dns.resolver
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from foodlog_backend.mail_authentication import (
    DkimMailAuthenticator,
    MailAuthenticationTemporaryFailure,
)
from foodlog_backend.models import RawMailAuthenticationOutcome

ACCOUNT_ID = "account-a"
MAIL_ID = "a" * 64
SELECTOR = b"foodlog-test"


@pytest.fixture(scope="module")
def dkim_key() -> tuple[bytes, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    dns_record = b"v=DKIM1; k=rsa; p=" + b64encode(public_der)
    return private_pem, dns_record


def message_bytes() -> bytes:
    message = EmailMessage(policy=SMTP)
    message["From"] = "Nemlig <kontakt@nemlig.com>"
    message["To"] = "foodlog@example.test"
    message["Subject"] = "Tak for din ordre"
    message.set_content("Tak for din ordre\nOrdrenummer: 9000000001\n")
    return message.as_bytes()


def signed_message(
    private_key: bytes,
    *,
    domain: bytes = b"nemlig.com",
    include_headers: tuple[bytes, ...] = (
        b"from",
        b"to",
        b"subject",
        b"mime-version",
        b"content-type",
        b"content-transfer-encoding",
    ),
    length: bool = False,
) -> bytes:
    raw_message = message_bytes()
    signature = dkim.sign(
        raw_message,
        selector=SELECTOR,
        domain=domain,
        privkey=private_key,
        include_headers=list(include_headers),
        signature_algorithm=b"rsa-sha256",
        length=length,
    )
    return signature + raw_message


def authenticate(raw_message: bytes, dns_record: bytes):
    expected_name = SELECTOR + b"._domainkey.nemlig.com."

    def dnsfunc(name: bytes, *, timeout: int) -> bytes | None:
        assert timeout == 3
        return dns_record if name == expected_name else None

    return asyncio.run(
        DkimMailAuthenticator(dnsfunc=dnsfunc).authenticate(
            raw_message,
            account_id=ACCOUNT_ID,
            raw_mail_id=MAIL_ID,
        )
    )


def test_valid_aligned_signature_authenticates_exact_message(dkim_key) -> None:
    private_key, dns_record = dkim_key
    raw_message = signed_message(private_key)

    result = authenticate(raw_message, dns_record)

    assert result.outcome == RawMailAuthenticationOutcome.ALIGNED_DKIM_PASS
    assert result.signer_domain == "nemlig.com"
    assert {"from", "subject", "content-type"}.issubset(result.signed_headers)


@pytest.mark.parametrize(
    "raw_message",
    [
        message_bytes(),
        message_bytes().replace(
            b"Subject: Tak for din ordre\r\n",
            b"Subject: Tak for din ordre\r\n"
            b"Authentication-Results: attacker.invalid; dkim=pass header.d=nemlig.com; "
            b"dmarc=pass header.from=nemlig.com\r\n",
        ),
        message_bytes().replace(
            b"Subject: Tak for din ordre\r\n",
            b"Subject: Tak for din ordre\r\n"
            b"ARC-Authentication-Results: i=1; attacker.invalid; "
            b"dkim=pass header.d=nemlig.com\r\n",
        ),
    ],
)
def test_unsigned_and_sender_authentication_headers_are_untrusted(
    dkim_key,
    raw_message: bytes,
) -> None:
    _, dns_record = dkim_key

    result = authenticate(raw_message, dns_record)

    assert result.outcome == RawMailAuthenticationOutcome.UNTRUSTED


def test_body_mutation_breaks_authentication(dkim_key) -> None:
    private_key, dns_record = dkim_key
    raw_message = signed_message(private_key).replace(
        b"9000000001",
        b"9000000002",
    )

    result = authenticate(raw_message, dns_record)

    assert result.outcome == RawMailAuthenticationOutcome.UNTRUSTED


def test_unaligned_or_insufficient_signature_is_untrusted(dkim_key) -> None:
    private_key, dns_record = dkim_key
    unaligned = signed_message(private_key, domain=b"attacker.example")
    unsigned_subject = signed_message(
        private_key,
        include_headers=(
            b"from",
            b"to",
            b"mime-version",
            b"content-type",
            b"content-transfer-encoding",
        ),
    )
    partial_body = signed_message(private_key, length=True)

    assert authenticate(unaligned, dns_record).outcome == RawMailAuthenticationOutcome.UNTRUSTED
    assert (
        authenticate(unsigned_subject, dns_record).outcome
        == RawMailAuthenticationOutcome.UNTRUSTED
    )
    assert authenticate(partial_body, dns_record).outcome == RawMailAuthenticationOutcome.UNTRUSTED


def test_dns_timeout_is_retryable(dkim_key) -> None:
    private_key, _ = dkim_key
    raw_message = signed_message(private_key)

    def dnsfunc(name: bytes, *, timeout: int) -> bytes | None:
        del name, timeout
        raise dkim.DnsTimeoutError("test timeout")

    with pytest.raises(MailAuthenticationTemporaryFailure, match="dkim_dns_failure"):
        asyncio.run(
            DkimMailAuthenticator(dnsfunc=dnsfunc).authenticate(
                raw_message,
                account_id=ACCOUNT_ID,
                raw_mail_id=MAIL_ID,
            )
        )


def test_nameserver_failure_is_retryable(dkim_key) -> None:
    private_key, _ = dkim_key
    raw_message = signed_message(private_key)

    def dnsfunc(name: bytes, *, timeout: int) -> bytes | None:
        del name, timeout
        raise dns.resolver.NoNameservers

    with pytest.raises(MailAuthenticationTemporaryFailure, match="dkim_dns_failure"):
        asyncio.run(
            DkimMailAuthenticator(dnsfunc=dnsfunc).authenticate(
                raw_message,
                account_id=ACCOUNT_ID,
                raw_mail_id=MAIL_ID,
            )
        )
