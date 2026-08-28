# Nemlig mail classification v1

This contract was checked on 26 August 2026 against two real Nemlig messages in a
read-only mailbox. Only redacted structural observations are recorded here; the real
messages, identifiers, customer details, addresses, and purchase contents were not
copied into the repository.

## Observed purchase-document shapes

| Document | Subject | Body markers | Attachment |
| --- | --- | --- | --- |
| Order confirmation | `Tak for din ordre` | `Tak for din ordre`, editable-order wording, then `Ordrenummer:` followed by a 10-digit identifier | None |
| Final invoice | `Faktura - ##########` | `Din ordre er på vej` and an attached-invoice statement | One `Faktura - ##########.pdf`; declared `application/octet-stream`, decoded as PDF |

The redacted identifier in the reviewed final-invoice subject was exactly equal to the
retailer-labelled order number in its earlier confirmation. The classifier therefore
uses that exact value for both final-document aliases, allowing the immutable
confirmation and authoritative final invoice to become ordered revisions of one
purchase. It never links messages by dates, totals, product similarity, or timing.

## Trust and fallback behavior

Purchase promotion requires all of the following:

- the exact normalized sender `kontakt@nemlig.com`;
- a cryptographically verified DKIM signature aligned to `nemlig.com` or one of its
  subdomains, covering `From`, `Subject`, and the top-level MIME interpretation headers;
- one of the two exact document structures above;
- a bounded retailer-labelled numeric identifier;
- for a final invoice, a matching PDF filename and decoded PDF signature.

Delivery updates, surveys, credit notes, support messages, incomplete purchase shapes,
or messages without aligned authentication remain raw retained mail and do not become
purchase evidence. Email prose is passive input, never agent instructions.

The private worker verifies the exact retained MIME bytes against the signer's DNS key,
rejects partial-body (`l=`) signatures, and persists an immutable verdict bound to the
account, raw-mail ID, and content hash. Both the classifier and the purchase-attachment
transaction require that verdict. Sender-authored `Authentication-Results` and
`ARC-Authentication-Results` headers are ignored. Resolver timeouts are retried; missing,
invalid, or unaligned signatures are durably untrusted.

An authenticated final-invoice shape is not purchase evidence until its selected PDF
also parses inside the bounded invoice subprocess. Oversized, malformed, encrypted, or
resource-exhausting PDFs receive a durable terminal-rejection disposition and create no
purchase document. Infrastructure or child-protocol failures remain retryable.

The committed `.eml` fixtures contain synthetic identifiers and content while
preserving the observed MIME declaration, labels, and document relationship. Their
invented authentication headers are deliberately not trusted; cryptographic verifier
tests generate ephemeral keys at runtime and commit no private key.
