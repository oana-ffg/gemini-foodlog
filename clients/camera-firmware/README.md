# FoodLog physical-camera firmware

This package is the C++ core for the M5Stack Unit CamS3-5MP U174-B firmware. It
shares the deployed Capture API v1 contract with the browser and Python clients.

The current checkpoint contains the hardware-independent motion and persistent
delivery state machines. They are compiled and tested on the host so cadence,
retry, reboot, capacity, and permanent-failure behavior do not depend on a camera
being attached. The ESP-IDF adapters for PY260 capture, encrypted NVS, encrypted
microSD storage, USB provisioning, Wi-Fi, trusted time, and HTTPS remain blocked
on the exact physical board and are not represented as implemented.

Run the portable tests:

```sh
make test
```

The core starts a capture immediately when motion is observed, captures no more
than once per second during a 15-second motion burst, samples once per minute while
the activity remains open, and returns to watching after five minutes without
motion. These defaults match the browser client and remain configuration values
for later real-kitchen calibration.

The queue persists an atomic metadata snapshot through a storage interface. The
ESP-IDF storage adapter must durably write encrypted image bytes before adding the
item to that snapshot and remove them only after the snapshot no longer references
them. Authentication and trial-quota failures block all delivery; an explicitly
authorized recovery resumes the same oldest item. Invalid item payloads are
dropped with a durable counter rather than retried forever. Capacity loss also has
a durable counter.

See [the physical camera design](../../docs/physical-camera-design.md) for the
hardware, provisioning, trust, encryption, and bench-test decisions.
