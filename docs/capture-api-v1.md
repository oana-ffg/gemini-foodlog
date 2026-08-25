# Capture API v1

This is the shared client contract for browser capture, the Python simulator, and
the physical camera. The `CAP-002` implementation exposes it as one multipart
`POST /v1/captures` request; it is not a live production endpoint until that ticket
is deployed.

## Authentication

- Browser clients send a verified Firebase ID token as
  `Authorization: Bearer <id-token>` and, once enabled, the Firebase App Check
  token required by the hosted API.
- Physical cameras and the Python simulator send the one-time provisioned secret
  as `Authorization: FoodLogCamera <credential>`.
- Clients never send an account or owner identifier. The backend derives both from
  the verified user or camera credential and verifies that `camera_id` matches it.

## Request

The request is `multipart/form-data` with:

- `metadata`: UTF-8 JSON matching
  [`capture-envelope-v1.schema.json`](../contracts/capture-envelope-v1.schema.json);
- `image`: one non-empty JPEG or PNG whose actual bytes match its declared media
  type, dimensions match the envelope, and encoded size is at most 5 MiB;
- `Idempotency-Key`: an 8–128 character client-generated header reused only when
  retrying the exact same camera frame and metadata.

`captured_at` includes a UTC offset. `sequence_id` identifies a client boot or
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
