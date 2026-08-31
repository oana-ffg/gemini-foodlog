# Physical camera target and provisioning design

## Decision

The MVP prototype targets the **Freenove ESP32-S3 WROOM Board with camera,
product code FNK0085**. This is the exact USB-connected board available for the
hackathon demo, and its previous Home Camera firmware has a separately validated
camera-only restore bundle. The FoodLog implementation uses Arduino through the
pinned PlatformIO `freenove_esp32_s3_wroom` target because that target and camera
pin map have already run on this physical unit.

PlatformIO identifies this target as an ESP32-S3 N8R8 board with 8 MB flash and
8 MB PSRAM. FoodLog captures 640-by-480 frames for local motion analysis and
switches to the camera sensor's 1600-by-1200 JPEG mode for accepted uploads when
PSRAM is available. The backend's 4096-by-4096 envelope accepts both sizes.

The camera must be mounted away from stove heat, steam, and splashes. Its final
side view, image focus, Wi-Fi stability, and temperature still require the planned
human kitchen-position test.

Official references:

- [Freenove FNK0085 source, examples, and purchase links](https://github.com/Freenove/Freenove_ESP32_S3_WROOM_Board)
- [Freenove FNK0085 documentation](https://docs.freenove.com/projects/fnk0085/en/latest/index.html)
- [Freenove camera web-server example](https://docs.freenove.com/projects/fnk0085/en/latest/fnk0085/codes/C/32_Camera_Web_Server.html)
- [ESP32-S3 platform security overview](https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/security/security.html)

## Alternatives considered

| Board | Decision | Reason |
| --- | --- | --- |
| Freenove FNK0085 ESP32-S3 WROOM N8R8 | Selected for MVP | The exact backed-up board is connected, its camera wiring and motion calibration are physically proven, and it has 8 MB PSRAM plus USB serial provisioning. |
| M5Stack Unit CamS3-5MP U174-B | Future product candidate | Enclosed and higher resolution, but it is not the board available for the hackathon bench proof and would require a second hardware adapter. |
| Seeed XIAO ESP32S3 Sense | Reserve option | Compact, but camera revisions vary and it needs an enclosure and mount. |
| Original ESP32-CAM boards | Reject | Less memory and a weaker provisioning/debugging baseline than the available ESP32-S3 board. |

## Trust boundary

The physical camera can only:

- validate its own credential through `GET /v1/device/status`;
- poll its own pending command through `GET /v1/device/snapshot-request`; and
- upload a frame through `POST /v1/captures` using
  `Authorization: FoodLogCamera <credential>`.

It cannot read images, meals, purchases, questions, account data, another camera,
or administrative state. The backend derives account and camera scope from the
random credential. Firmware never sends an account or owner identifier. Website
snapshot creation and status routes require the verified owner's Firebase bearer
token, return 404 across account boundaries, and use private no-store responses.
Revoking one device credential does not affect the owner's session or other
cameras.

The firmware replaces the prior device image and does not use a vendor cloud.
No audio is captured.

## One-time provisioning

Provisioning is local and requires physical USB access. The device does not expose
an open setup access point, BLE pairing surface, or reusable factory secret.

1. A verified owner creates a named physical camera in FoodLog.
2. The backend returns the camera ID and one `flc_v1_` credential exactly once in
   a `Cache-Control: no-store` response. Only its SHA-256 verifier is retained by
   the backend.
3. The owner downloads a private JSON setup file and the public setup ZIP.
4. `setup-foodlog-camera.ps1` verifies the packaged firmware SHA-256, creates a
   user-local Python environment, and installs pinned `esptool` and `pyserial`
   versions. It prompts when more than one COM port exists.
5. `provision_camera.py` validates that the setup file targets the exact FoodLog
   HTTPS origin, then asks interactively for Wi-Fi details. Secrets never enter
   command-line arguments or logs.
6. The camera accepts four base64-transported, length-bounded serial fields between
   `PROVISION_BEGIN` and `PROVISION_COMMIT`. Neither side echoes values. Firmware
   validates the complete record before one NVS commit and reboot.
7. After trusted NTP time is available, the camera validates normal TLS hostname
   and chain trust using the four public GTS roots derived from Google's official
   `https://pki.goog/roots.pem`, then calls `/v1/device/status`.

The production API origin is compiled into firmware. It cannot be replaced by a
setup file. Root-CA trust is used instead of disabling certificate validation or
pinning a rotating service leaf certificate. The board's known-good explicit
IP-plus-SNI TLS overload preserves hostname verification after a separate DNS
lookup.

## Prototype secret and image storage

This development build stores Wi-Fi and camera credentials in ESP32 NVS without
hardware flash encryption. The board must remain physically controlled. Secure
Boot v2, release-mode flash encryption, and HMAC-backed NVS encryption are release
hardening work and must only be enabled after recovery is proven because those
eFuse changes are irreversible.

Firmware 0.2.0 persists the most recent 100 unsent captures to the onboard
microSD card. JPEG bytes, capture metadata, and stable idempotency identity are
encrypted and authenticated with AES-256-GCM under a device-specific key derived
from the high-entropy camera credential. Each record is committed with
write-flush-rename, interrupted temporary files are removed at startup, delivery
is oldest-first with exponential backoff capped at one minute, and acknowledged
or permanently invalid items are deleted. When capacity is reached, the oldest
unsent item is evicted and the loss is reported over serial so the card retains
the latest evidence.

Every boot runs a real encrypted write/read/authentication/delete card self-test.
If the card is missing or unhealthy, online transfer remains available but the
firmware refuses a plaintext persistence fallback. The CAP-012 hardware gate was
proven with a private manual JPEG committed before an externally forced ESP32
reset, recovered as one queued item after reboot, accepted by the backend under
the original idempotency identity, and removed from the card only afterward.

## Firmware behavior

- A brightness-compensated 80-by-60 luminance decoder uses the motion calibration
  proven on this exact camera: normalized average score `2/255`, changed-pixel
  ratio `0.025`, and two consecutive positive frames.
- Confirmed motion captures immediately, then at most once per second during a
  15-second burst, once per minute while activity remains open, and closes after
  five minutes without motion. The portable host tests own this cadence.
- The device polls every two seconds for one owner-created manual snapshot. It
  includes the opaque request ID in the normal Capture API envelope; the backend
  completes only a matching request for that credential's camera.
- Captures include UTC time, dimensions, firmware version, sequence identity,
  burst identity/index where applicable, and bounded motion metadata.
- TLS uploads use stable idempotency keys and three immediate retries. Revoked
  credentials and trial quota stop capture until the periodic status check proves
  recovery; invalid requests are not retried forever.
- A cold boot pauses upload until authenticated network time is available instead
  of inventing `captured_at`.

## Bench gate

The exact board gate is:

1. compile the firmware for `freenove_esp32_s3_wroom` with its embedded trust
   bundle;
2. flash the connected board over USB without administrator access if the current
   serial driver permits it;
3. provision without a secret appearing in process arguments, logs, or serial
   echo, then prove NVS persistence across reboot;
4. validate the dedicated test-account camera credential against production TLS;
5. request one private snapshot from the signed-in website and verify the resulting
   capture identity and image;
6. verify motion telemetry and a real hand-wave trigger with Oana;
7. prove the encrypted queue across an externally interrupted power cycle,
   byte-identical retry, and truthful drop accounting;
8. verify focus, colour visibility, Wi-Fi reliability, and safe placement in the
   intended kitchen position.
