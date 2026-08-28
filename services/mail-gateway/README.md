# FoodLog inbound mail gateway

This is the default App Engine Standard service that receives raw MIME requests for
the account-specific `appspotmail.com` addresses. It performs only the transport
boundary work: recipient resolution, immutable raw storage, idempotent Firestore
state, and publication of a small `raw_mail_stored` event. Invoice parsing and agent
reasoning run later in the private worker.

## Local verification

```sh
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Regenerate `requirements.txt` after an intentional lock change:

```sh
uv export --frozen --no-dev --no-hashes --no-header --no-emit-project \
  --output-file requirements.txt
```

The Firestore transaction test runs when `FIRESTORE_EMULATOR_HOST` is present. The
root Firebase emulator command can provide a fresh instance for it.

`scripts/smoke_production.py` is an explicit-write operator check: it sends one
clearly labelled benign synthetic MIME through the production adapters, verifies its
exact retry and private bytes, then proves an executable attachment leaves no record
or object. Run it only against the dedicated test account after reviewing `--help`;
the generated Pub/Sub test event must be verified and acknowledged afterward.

`scripts/smoke_purchase_worker.py` is the explicit-write production rejection check.
It sends synthetic Nemlig-shaped confirmation and invoice fixtures containing forged
passing authentication-result headers, waits for the private worker's immutable
`untrusted` verdicts, and proves that neither message becomes purchase evidence. A
positive production check requires a real forwarded, cryptographically signed Nemlig
message; no synthetic fixture or committed private signing key can establish that
boundary. The check never invokes Gemini.

## Production deployment

`app.yaml` is deliberately the default service because App Engine delivers inbound
mail handlers only there. It enables `inbound_services: mail`, restricts handlers to
App Engine internal/admin requests, uses the dedicated keyless mail identity, and
caps automatic scaling at one F1 instance with a zero minimum.

Every message is external, untrusted evidence. Before any durable write, the gateway
first resolves an active opaque recipient and atomically charges that account's
request/byte rate window. It then requires one valid sender, bounded singleton transport headers, a structurally valid
MIME tree, at most 100 parts/eight levels/20 attachments, and an allowlist limited to
plain text, HTML, common inline images, and PDF. Nemlig's observed octet-stream invoice
declaration is accepted only for a `.pdf` attachment whose decoded bytes have a PDF
signature. Other active or unknown content types, unsafe filenames, unknown transfer
encodings, malformed MIME, and oversized headers or attachments are discarded with no
object, record, or event. Instruction-like text
is not heuristically interpreted or filtered: accepted bytes remain opaque in private
storage, while the metadata-only event carries `trust_class=untrusted_external` so a
later parser or agent must treat them as data, never instructions.

After validation, a second transaction revalidates the exact active address generation
and reserves retained capacity with the idempotent raw-mail record. The defaults are
400 retained messages and 256 MiB retained bytes per account, plus 30 requests and
64 MiB of request bodies per one-hour window. Pending writes count toward retained
capacity; exact retries consume rate capacity but not retained capacity twice. Stored
ceilings are lower-wins, so configuration can tighten them but cannot silently raise
them. Accepted raw mail is retained indefinitely for the MVP; the hard per-account
storage limits keep that choice bounded until the fixed-expiry post-hackathon work.

The authenticated owner can rotate or revoke the address. Both operations atomically
revoke the old route without a grace overlap, and revoked route tombstones are retained
so an address is never reused. Rotation requires updating the sender-side forwarding
rule.

Before the first quota-aware production traffic cutover, grant the gateway its
source-controlled one-permission raw-object verifier role. Promote the quota-aware
version first so accounts with pre-existing mail fail retryably while their ledger is
absent, then run:

```sh
uv run python scripts/backfill_usage.py \
  --project gemini-foodlog-2026 \
  --bucket gemini-foodlog-2026-raw-mail-163029863855
```

The backfill requires exact equality between Firestore metadata and every account's
raw-mail object prefix, rejects objects belonging to unknown accounts, verifies every
present object's size and SHA-256, transactionally proves the raw-mail snapshot did
not change, and creates exact pending/retained counters. Existing data above either
hard cap blocks deployment and requires an explicit retention decision; the migration
never deletes or silently exempts it.
Never backfill while an older gateway version can still accept unaccounted mail. Verify
every existing account has `inbound_mail_usage/current` before the transport smoke.

Review the upload manifest before deploying. Deploy a unique version without
promoting it, inspect its configuration and logs, then move traffic only after the
version is healthy. Actual `/_ah/mail/...` verification must use App Engine's inbound
mail transport; an ordinary external POST is rejected by the `login: admin` handler.
