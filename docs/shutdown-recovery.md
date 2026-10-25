# Paused deployment and recovery

FoodLog's hosted deployment was paused on **25 October 2026**, at the owner's request after hackathon judging. Project billing was subsequently disabled after a verified private offline backup. The public repository and [recorded demo](https://vimeo.com/1222721564) remain available.

## Current state

- Production and preview Cloud Run services use manual scaling with zero instances. No traffic tags bypass the pause.
- Firebase Hosting is disabled; the hosted frontend returns HTTP 404. Public Cloud Run API and preview health endpoints return HTTP 503.
- App Engine is disabled, including the inbound-mail gateway.
- Pub/Sub push delivery is paused. Subscriptions remain present; all queues reported zero pending messages before suspension.
- A verified offline archive preserves the database export, storage object generations, recoverable soft-deleted staging files, container-image graphs, account/password-hash recovery data, secret-version payloads, exact authored frontend files, configuration, logs and source history. It includes a self-contained offline verifier and restore instructions.
- The project is unlinked from its billing account; live billing readback confirms `billingEnabled = false`. The Google Cloud project was not deleted.

Google can remove cloud resources while billing is disabled, so recovery must rely on the verified offline archive. Previously accrued charges can still be invoiced. See [Google's billing-disable behavior](https://docs.cloud.google.com/billing/docs/how-to/modify-project).

## Infrastructure configuration

`runtime_suspended = true` in `infra/terraform/production/production.auto.tfvars` records the pause. Terraform controls Cloud Run service-level manual scaling and the five Pub/Sub push configurations. The Production workflow rejects deploy operations while this setting is true. App Engine serving status and Firebase Hosting releases continue to be managed outside this Terraform root.

Before billing was disabled, Terraform formatting, validation, and a full-refresh zero-change plan passed after the runtime shutdown. No application release image, ready revision, or container definition changed.

## Resuming

1. Verify the offline archive, confirm the operating budget and review current model availability, dependencies and credentials. Re-enable project billing only when deliberately resuming.
2. Inspect which cloud resources still exist. Use the offline restore guide to recover removed data or artifacts into a reviewed target. Preserve the archive and avoid overwriting surviving data.
3. Review the pinned Terraform configuration against that target. Keep `runtime_suspended = true` while restoring infrastructure, images, secrets, database/index definitions and stored objects. Only then set it to `false` and review/apply a fresh plan to restore automatic scaling and authenticated Pub/Sub push delivery. Do not use an old saved plan.
4. Enable the existing App Engine application in its application settings. Retain the previous reviewed version and traffic configuration unless deliberately releasing an update.
5. Restore a retained Firebase Hosting version through the [Hosting releases API](https://firebase.google.com/docs/reference/hosting/rest/v1beta1/sites.releases/create), or deploy an explicitly reviewed current web build.
6. Verify signed-out privacy boundaries, login, API health, image access, queue delivery and journal access. Choose any real uploads, paid model processing or email tests deliberately. Confirm a full zero-change Terraform plan, then update the deployment status in the README, GitHub description and backlog.

## Provider behavior

Google documents [manual zero-instance Cloud Run scaling](https://docs.cloud.google.com/run/docs/configuring/services/manual-scaling) as disabling a service while allowing current requests to finish. Traffic-tag-only revisions are excluded, so shutdown checks must inspect tags. [Disabling App Engine](https://docs.cloud.google.com/appengine/docs/standard/python3/building-app/cleaning-up) retains its data. Firebase's [release resource](https://firebase.google.com/docs/reference/hosting/rest/v1beta1/sites.releases) supports a `SITE_DISABLE` release without deleting the stored version.
