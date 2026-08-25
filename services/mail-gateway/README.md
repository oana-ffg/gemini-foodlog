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

## Production deployment

`app.yaml` is deliberately the default service because App Engine delivers inbound
mail handlers only there. It enables `inbound_services: mail`, restricts handlers to
App Engine internal/admin requests, uses the dedicated keyless mail identity, and
caps automatic scaling at one F1 instance with a zero minimum.

Review the upload manifest before deploying. Deploy a unique version without
promoting it, inspect its configuration and logs, then move traffic only after the
version is healthy. Actual `/_ah/mail/...` verification must use App Engine's inbound
mail transport; an ordinary external POST is rejected by the `login: admin` handler.
