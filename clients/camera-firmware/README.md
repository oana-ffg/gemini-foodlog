# FoodLog physical-camera firmware

This package builds the FoodLog prototype firmware for the Freenove ESP32-S3
WROOM board sold as FNK0085. It shares Capture API v1 with the browser and Python
clients and uses the board's OV2640-compatible camera wiring.

The board firmware includes local JPEG-luma motion detection, the shared capture
cadence state machine, Wi-Fi and trusted-time setup, HTTPS upload with an embedded
root bundle, owner-requested snapshot polling, and a secret-safe USB provisioning
protocol. The portable state-machine tests remain host-runnable.

Run the portable tests with a C++17 compiler:

```sh
make test
```

Build the exact board image with PlatformIO:

```sh
pio run -d clients/camera-firmware
```

The core starts a capture immediately when motion is observed, captures no more
than once per second during a 15-second motion burst, samples once per minute while
the activity remains open, and returns to watching after five minutes without
motion. These defaults match the browser client and remain configuration values
for later real-kitchen calibration.

Firmware 0.2.0 mounts the onboard microSD card in one-bit SDMMC mode and keeps
the most recent 100 unsent captures in an application-encrypted queue. Each JPEG,
its metadata, and its stable idempotency key are protected with AES-256-GCM under
a device-specific key derived from the high-entropy camera credential. Files are
committed through write, flush, and rename; startup removes interrupted temporary
files, delivery is oldest-first with bounded exponential backoff, and an item is
deleted only after backend acknowledgement or a permanent item rejection. A full
queue evicts the oldest unsent capture and reports the loss over serial.

The card performs an encrypted write/read/authentication/delete self-test at
every boot. If the card is missing or fails, online capture remains available but
the firmware refuses to persist private images without encryption. Credentials
remain in ESP32 NVS without hardware flash encryption, so do not deploy this
prototype in an untrusted physical location until the irreversible release
hardening workflow is separately proven.

See [the physical camera design](../../docs/physical-camera-design.md) for the
hardware, provisioning, trust, encryption, and bench-test decisions.
