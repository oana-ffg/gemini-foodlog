# Private Cloud Run preview

This preview exists only to smoke-test a committed backend revision on Google Cloud before the durable production adapters are ready.

## Security and cost boundary

- Cloud Run rejects unauthenticated callers through IAM.
- The application independently requires a generated preview secret stored in Secret Manager.
- Gemini and every other external model call remain disabled; inference only recognizes immutable local fixture hashes.
- The service has zero minimum instances and one maximum instance.
- Account, image, meal, feedback, and question state is in memory and may disappear on any cold start or revision change.
- Production configuration still fails closed until the private GCS, Firestore, Firebase Authentication, and worker adapters exist.

The preview is not a public trial, production deployment, or durability claim. Its purpose is an authenticated live smoke test and Cloud Run reporting verification.

## Live resources

The exact live service URL, revision, generated test-account identifier, smoke-test evidence, and reporting outcome are recorded here only after they are observed successfully. Secrets and bearer tokens are never recorded.
