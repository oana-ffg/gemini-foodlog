# Physical camera target and provisioning design

## Decision

The MVP physical client targets the **M5Stack Unit CamS3-5MP, SKU U174-B**, using
ESP-IDF and C++. No hardware has been purchased by this repository work.

On 28 August 2026, the current official product is a USD 17.50
ESP32-S3-WROOM-1-N16R8 unit with 8 MB
PSRAM, 16 MB flash, a 5 MP PY260 fixed-focus JPEG camera, an 88-degree field of
view, a microSD slot, an onboard Wi-Fi antenna, and bundled Grove-to-USB-C
programming hardware. Its maximum documented image size is 2592 by 1944, within
the backend's 4096-by-4096 envelope. The older 2 MP U174 is end-of-life and must
not be substituted accidentally.

This is the best prototype fit because it is a currently sold, enclosed camera
with enough PSRAM for image capture, enough flash for two firmware slots plus
encrypted configuration, persistent removable storage, native ESP32-S3 security
features, and materially more distant-image detail than the discontinued 2 MP
model. It does not require a custom camera ribbon, enclosure, or separate SD
carrier.

The board's documented operating range is 0 to 40 degrees Celsius. It must be
mounted away from stove heat, steam, and splashes; the intended side view from
roughly two metres away is appropriate, but the bench test must still verify
temperature and Wi-Fi reliability in the actual position. Its onboard antenna is
the main hardware risk. It also has no documented battery-backed real-time clock,
so firmware must never invent wall-clock time after an offline cold boot.

Official references:

- [M5Stack product page and current price](https://shop.m5stack.com/products/unit-cams3-wi-fi-camera-5mp)
- [M5Stack hardware and ESP-IDF documentation](https://docs.m5stack.com/en/unit/Unit-CAMS3%205MP)
- [ESP32-S3 platform security overview](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/security/security.html)
- [ESP32-S3 security enablement workflows](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/security/security-features-enablement-workflows.html)

## Alternatives considered

| Board | Decision | Reason |
| --- | --- | --- |
| M5Stack Unit CamS3-5MP U174-B | Selected | Current, enclosed, 5 MP, 8 MB PSRAM, 16 MB flash, microSD, bundled USB adapter, official ESP-IDF example. |
| M5Stack Unit CamS3 U174 | Reject | The official store marks the 2 MP model end-of-life. |
| Seeed XIAO ESP32S3 Sense | Reserve option | Smaller and exposes an external antenna, but the camera changed from OV2640 to OV3660 across current revisions and it needs a separate enclosure and mount. |
| Espressif ESP32-S3-EYE | Reject for MVP | Excellent first-party development board, but its LCD, microphone, and accelerometer add unused cost and bulk while its camera is 2 MP and flash is 8 MB. |
| AI-Thinker ESP32-CAM / original ESP32 boards | Reject | Less memory and a weaker provisioning/debugging baseline than ESP32-S3; saving a few dollars is not worth increasing firmware and image-quality risk. |

## Trust boundary

The physical camera can only:

- validate its own credential through `GET /v1/device/status`;
- upload a frame through `POST /v1/captures` using
  `Authorization: FoodLogCamera <credential>`; and
- receive success or bounded error information for those requests.

It cannot read images, meals, purchases, questions, account data, another camera,
or administrative state. The backend derives account and camera scope from the
credential; firmware never sends an account or owner identifier. Revocation of
one device credential must not affect the owner's Firebase session or other
cameras.

The firmware completely replaces M5Stack's factory image and never uses EZData or
another vendor cloud. The microphone remains disabled and no audio is captured.

## One-time provisioning

Provisioning is local and requires physical USB access. The device does not expose
an open setup access point, BLE pairing surface, or reusable factory secret.

1. The verified owner creates a named physical camera in FoodLog with
   `POST /v1/device-cameras`.
2. The backend returns the camera ID and one `flc_v1_` credential exactly once in
   a `Cache-Control: no-store` response. Only its SHA-256 verifier is retained by
   the backend.
3. The owner connects the bundled Grove-to-USB-C adapter and opens a local USB
   serial provisioning session. A fresh device accepts its first configuration;
   an already provisioned device accepts replacement secrets but never reads the
   old ones back. Physical USB possession is the pairing boundary.
4. A local source-controlled provisioning command reads the Wi-Fi SSID, Wi-Fi
   passphrase, camera ID, camera credential, and an allowlisted local-time rule
   from interactive input, never command-line arguments. It sends one
   length-bounded versioned record over USB serial. Neither side echoes secret
   fields.
5. Firmware validates the complete record before one atomic commit to encrypted
   NVS, clears the serial receive buffer, reboots, joins Wi-Fi, and calls
   `/v1/device/status` over TLS.
6. The command reports only camera ID, firmware version, and success or a bounded
   error category. It never reads the stored secrets back. A failed partial write
   leaves the previous complete configuration active or the device unprovisioned.

TLS uses ESP-IDF's maintained certificate bundle and normal hostname validation;
the service certificate is not pinned because ordinary certificate rotation must
not brick the camera. The exact production API origin is compiled into signed
firmware rather than supplied by the device owner.

## Secret and queued-image storage

The final demo device uses ESP32-S3 Secure Boot v2, release-mode flash encryption,
and HMAC-backed NVS encryption. Its flash and NVS hardware keys are unique per
device; the firmware release-signing key never enters the repository. Espressif
recommends secure boot and flash encryption together and recommends NVS encryption
for stored Wi-Fi credentials.

Irreversible eFuse security settings are enabled only after the same physical board
passes firmware, camera, SD, recovery, and USB-update tests in development mode.
This avoids turning an ordinary firmware mistake into an unrecoverable board while
the client is still under active development.

Pending images on microSD are private user data. Each JPEG and queue manifest is
encrypted and authenticated at the application layer with AES-GCM using a
device-generated key retained only in encrypted NVS. Filenames contain opaque IDs,
not timestamps, account IDs, or meal guesses. An item contains the exact capture
envelope, content hash, and idempotency key needed for a byte-identical retry. It
is removed logically only after a successful backend acknowledgement. Queue
eviction deletes the oldest encrypted item and increments a durable dropped-frame
counter; ordinary SD deletion is not represented as forensic secure erasure.

The persistent-queue capacity is selected from measured JPEG sizes on the actual
board and SD card during CAP-012; it is not guessed from the card's nominal size.

## Firmware and delivery behavior

- ESP-IDF with C++ owns camera, Wi-Fi, TLS, USB serial, encrypted NVS, SD, and task
  supervision. Arduino is not the production firmware framework.
- Motion detection uses low-resolution frames locally. Accepted burst frames are
  encoded as JPEG and sent through the existing Capture API v1 contract.
- Sequence number, burst identity, timestamp with UTC offset, dimensions, firmware
  version, motion score, threshold, and algorithm name follow the checked-in JSON
  schema exactly.
- The device persists its sequence state before acknowledging local capture, sends
  oldest-first, and reuses the same idempotency key and bytes on retry.
- Transient network and server failures back off with jitter. Revoked credential,
  exhausted trial quota, invalid payload, and other permanent responses stop that
  retry loop and remain locally visible through status LED/serial diagnostics.
- Watchdog resets and power loss cannot turn a queued item into a different upload
  or silently reset the dropped-frame counter.
- Each boot must establish trusted time over authenticated network service before
  starting capture. During a later network outage, wall time is derived from that
  trusted anchor and the monotonic clock. A cold boot without time sync pauses
  capture visibly instead of fabricating `captured_at`; the provisioned local-time
  rule supplies the UTC offset preserved in the capture envelope.

## Bench gate

CAP-011 remains unverified until the exact U174-B board is physically available.
The gate is:

1. flash the FoodLog firmware through the bundled adapter;
2. provision over USB without a secret appearing in process arguments, logs, or
   serial echo;
3. reboot and prove encrypted configuration persists;
4. validate the device credential against production over TLS;
5. upload an exact JPEG and verify its server-side SHA-256 and camera identity;
6. revoke the camera and prove status/upload receive permanent authentication
   failure without an infinite retry;
7. disconnect Wi-Fi and power during queued delivery, then prove reboot recovery,
   byte-identical idempotent upload, and truthful drop accounting;
8. run in the intended kitchen position long enough to verify image focus, meat
   colour visibility, Wi-Fi stability, power draw, SD behavior, trusted-time
   recovery, and temperature below the board's 40-degree maximum.
