# Paused deployment and recovery

FoodLog's hosted deployment was paused on **25 October 2026**, at the owner's request after hackathon judging. The public repository and [recorded demo](https://vimeo.com/1222721564) remain available.

## Current state

- Production and preview Cloud Run services use manual scaling with zero instances. No traffic tags bypass the pause.
- Firebase Hosting is disabled; the hosted frontend returns HTTP 404. Public Cloud Run API and preview health endpoints return HTTP 503.
- App Engine is disabled, including the inbound-mail gateway.
- Pub/Sub push delivery is paused. Subscriptions remain present; all queues reported zero pending messages before suspension.
- Firestore, stored application files, container images, deployed versions, secret versions, identities, and Terraform state remain available. The database is deletion-protected and remains readable independently of App Engine.
- A private archive of the previous runtime configurations and recovery release identifiers is retained in private cloud storage. This archive contains configuration metadata; original application data remains in its existing stores.

Billing remains attached to preserve data and recovery resources. Retained storage and artifacts may still incur charges. This is a reversible runtime pause, not project deletion or a zero-cost guarantee.

## Infrastructure configuration

`runtime_suspended = true` in `infra/terraform/production/production.auto.tfvars` records the pause. Terraform controls Cloud Run service-level manual scaling and the five Pub/Sub push configurations. The Production workflow rejects deploy operations while this setting is true. App Engine serving status and Firebase Hosting releases continue to be managed outside this Terraform root.

Terraform formatting, validation, and a full-refresh zero-change plan passed after shutdown. No application release image, ready revision, or container definition changed.

## Resuming

1. Confirm the intended operating budget and review current model availability, dependencies, credentials, retained data and deployment artifacts.
2. Retrieve the private recovery configuration if needed. Preserve the original data; do not destroy or recreate data resources to resume the application.
3. Set `runtime_suspended = false` in the checked-in production variables. Initialize the pinned Terraform version and review a fresh plan before applying it. Expected runtime changes restore automatic scaling for seven services and the five original authenticated Pub/Sub push configurations.
4. Enable the existing App Engine application in its application settings. Retain the previous reviewed version and traffic configuration unless deliberately releasing an update.
5. Restore a retained Firebase Hosting version through the [Hosting releases API](https://firebase.google.com/docs/reference/hosting/rest/v1beta1/sites.releases/create), or deploy an explicitly reviewed current web build.
6. Verify signed-out privacy boundaries, login, API health, image access, queue delivery and journal access. Choose any real uploads, paid model processing or email tests deliberately. Confirm a full zero-change Terraform plan, then update the deployment status in the README, GitHub description and backlog.

## Provider behavior

Google documents [manual zero-instance Cloud Run scaling](https://docs.cloud.google.com/run/docs/configuring/services/manual-scaling) as disabling a service while allowing current requests to finish. Traffic-tag-only revisions are excluded, so shutdown checks must inspect tags. [Disabling App Engine](https://docs.cloud.google.com/appengine/docs/standard/python3/building-app/cleaning-up) retains its data. Firebase's [release resource](https://firebase.google.com/docs/reference/hosting/rest/v1beta1/sites.releases) supports a `SITE_DISABLE` release without deleting the stored version.
