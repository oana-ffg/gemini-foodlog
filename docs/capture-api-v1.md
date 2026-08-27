# Capture API v1

This is the shared client contract for browser capture, the Python simulator, and
the physical camera. Production exposes it at
`https://foodlog-api-sptvo5nsga-ew.a.run.app/v1/captures` as one multipart `POST`
request. The stable Cloud Run service URL stays the same across API revisions.

## Authentication

- Browser clients send a verified Firebase ID token as
  `Authorization: Bearer <id-token>`. Firebase App Check is deliberately deferred
  for the MVP under the documented cost boundary decision.
- Physical cameras and the Python simulator send the one-time provisioned secret
  as `Authorization: FoodLogCamera <credential>`.
- Clients never send an account or owner identifier. The backend derives both from
  the verified user or camera credential and verifies that `camera_id` matches it.

## Device provisioning and status

An authenticated, email-verified Firebase owner provisions a device with
`POST /v1/device-cameras` and a JSON body containing its name. The successful
response contains the camera record plus one `flc_v1_` credential and uses
`Cache-Control: no-store`; the plaintext credential cannot be retrieved again.

The device can validate its current credential with `GET /v1/device/status`.
The owner can independently revoke it with
`POST /v1/device-cameras/{camera_id}/revoke`. A revoked credential receives `401`
and must not be retried indefinitely.

## Browser registration and camera inventory

Each browser installation persists a random, non-secret `client_instance_id` and
registers it with its owner-chosen name through `POST /v1/browser-cameras`. The
backend stores only its SHA-256 hash and returns the same account-scoped camera on
retries, updating the name without creating duplicates. A different browser
installation creates an independent source.

An authenticated owner lists every browser and physical source, including revoked
ones, with `GET /v1/cameras`. `POST /v1/cameras/{camera_id}/revoke` revokes either
kind without affecting the owner's other cameras. Browser uploads use only active
browser camera IDs; physical credentials cannot be used through the browser path.

Every inventory item includes its accepted-capture count and nullable last-capture
time. Those activity fields update only after the image is durably stored and remain
unchanged for an exact idempotent retry.

## Request

The request is `multipart/form-data` with:

- `metadata`: UTF-8 JSON matching
  [`capture-envelope-v1.schema.json`](../contracts/capture-envelope-v1.schema.json);
- `image`: one non-empty JPEG or PNG whose actual bytes match its declared media
  type, fully decodes within 4096×4096 pixels, dimensions match the envelope, and
  encoded size is at most 5 MiB;
- `Idempotency-Key`: an 8–128 character client-generated header reused only when
  retrying the exact same camera frame and metadata.

`captured_at` includes a UTC offset and cannot be more than five minutes ahead of
the server. The backend derives and persists `captured_utc_offset_minutes` from that
parsed timestamp; clients cannot submit that internal field separately. Keeping the
original offset lets later household-pattern analysis use the camera's local calendar
day even though Firestore normalizes timestamps to UTC. Older offline-queued captures
remain valid. `sequence_id` identifies a client boot or
delivery sequence and `sequence_number` increases within it. The optional
`burst_id` and `burst_frame_index` are supplied together for a motion burst.
Periodic frames outside a burst omit both. Motion data is optional, explicitly
bounded, and descriptive; it does not override server-side authorization or event
grouping.

The server validates the authenticated camera, schema version, metadata bounds,
timestamp, MIME signature, decoded dimensions, idempotency relationship, and image
entitlement before accepting the frame. The checked-in
[`openapi.json`](../contracts/openapi.json) is generated deterministically from the
FastAPI application; backend tests fail when either contract artifact drifts.
