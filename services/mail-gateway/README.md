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

## Production deployment

`app.yaml` is deliberately the default service because App Engine delivers inbound
mail handlers only there. It enables `inbound_services: mail`, restricts handlers to
App Engine internal/admin requests, uses the dedicated keyless mail identity, and
caps automatic scaling at one F1 instance with a zero minimum.

Every message is external, untrusted evidence. Before any durable write, the gateway
requires one valid sender, bounded singleton transport headers, a structurally valid
MIME tree, at most 100 parts/eight levels/20 attachments, and an allowlist limited to
plain text, HTML, common inline images, and PDF. Active or unknown content types,
unsafe filenames, unknown transfer encodings, malformed MIME, and oversized headers
or attachments are discarded with no object, record, or event. Instruction-like text
is not heuristically interpreted or filtered: accepted bytes remain opaque in private
storage, while the metadata-only event carries `trust_class=untrusted_external` so a
later parser or agent must treat them as data, never instructions.

Review the upload manifest before deploying. Deploy a unique version without
promoting it, inspect its configuration and logs, then move traffic only after the
version is healthy. Actual `/_ah/mail/...` verification must use App Engine's inbound
mail transport; an ordinary external POST is rejected by the `login: admin` handler.
