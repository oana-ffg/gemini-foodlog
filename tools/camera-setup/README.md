# FoodLog camera setup utility

This utility flashes and provisions the **Freenove ESP32-S3 WROOM Board with
camera, product code FNK0085** for Gemini FoodLog. Other ESP32 camera boards have
different wiring and are not supported by this package.

The supported board, official examples, and current purchase links are maintained
by Freenove in the
[FNK0085 repository](https://github.com/Freenove/Freenove_ESP32_S3_WROOM_Board).

## What you need

- Windows 10 or 11
- Python 3.10 or newer
- a data-capable USB cable connected directly to the FNK0085 board
- a FAT-formatted microSD card in the board's onboard slot (the included card is
  suitable)
- 2.4 GHz Wi-Fi credentials (the ESP32 cannot join a 5 GHz-only network)
- the private camera setup JSON generated once by your signed-in FoodLog account

## Setup

1. In FoodLog, open **Camera → Add a physical camera**.
2. Enter a name, choose **Create physical camera**, and immediately choose
   **Download setup file**. The file contains the camera's one-time credential;
   do not email or share it.
3. Download and extract this ZIP. Do not run it from inside the ZIP preview.
4. Connect the FNK0085 board over USB.
5. Open PowerShell in the extracted folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup-foodlog-camera.ps1
   ```

6. Select the downloaded setup JSON and the camera's COM port when prompted.
   Enter the Wi-Fi name and password locally. Password input stays hidden.
7. Leave the camera powered. FoodLog will show the first accepted image after
   motion, or you can use **Take snapshot** beside the camera to test it.

The first run creates a private Python environment under
`%LOCALAPPDATA%\FoodLogCameraSetup` and installs pinned releases of Espressif's
flasher and pyserial. Administrator access is normally not required. Windows may
install a serial driver automatically when the board is first connected.

## Privacy and security

- Wi-Fi and camera credentials are sent only over the physically connected USB
  serial link, encoded as bounded fields, and are never printed by either side.
- The firmware accepts only the compiled FoodLog production HTTPS origin and uses
  normal hostname validation with the four public Google Trust Services roots
  derived from Google's official `https://pki.goog/roots.pem`.
- Every camera credential is scoped to one camera and one FoodLog account. It can
  upload images and poll its own manual-snapshot command, but cannot read images,
  meals, purchases, or another account.
- **Prototype limitation:** this development firmware stores Wi-Fi and camera
  credentials in ESP32 NVS without hardware flash encryption. Do not give the
  physical board to another person. The release-security fuse workflow must be
  completed before treating a lost board as tamper-resistant.
- Firmware 0.2.0 retains the most recent 100 unsent pictures on microSD. JPEGs,
  metadata, and retry identity are application-encrypted with AES-256-GCM and a
  device-specific key; incomplete writes are discarded on reboot and accepted
  pictures are removed after delivery. A missing or unhealthy card never causes
  a plaintext fallback: online transfer continues, but offline pictures are
  dropped with a diagnostic message.

## Recovery

- If no port appears, try another data-capable cable and reconnect the board.
- If flashing cannot enter download mode, hold **BOOT**, tap **RST**, release
  **BOOT**, and rerun the script.
- If Wi-Fi details change, rerun the utility with the same camera setup JSON.
- If the setup JSON is lost, revoke that camera in FoodLog, create a new physical
  camera, and download its new one-time setup file.
- To restore the earlier Home Camera firmware on Oana's prototype, use the
  separately validated camera-only restore bundle; this FoodLog package does not
  alter or delete that backup.
