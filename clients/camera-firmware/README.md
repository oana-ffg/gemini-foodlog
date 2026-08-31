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

The first hardware prototype retries a transient upload three times in memory.
Its encrypted persistent image queue and encrypted-at-rest credential store are
not implemented yet. Do not deploy this build in an untrusted physical location;
keep the board under household control until those hardening items land.

See [the physical camera design](../../docs/physical-camera-design.md) for the
hardware, provisioning, trust, encryption, and bench-test decisions.
