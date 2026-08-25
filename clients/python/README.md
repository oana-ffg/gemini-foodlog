# Python camera client

This standalone client replays reviewed image fixtures or captures a bounded webcam
sequence through the same production contract as the physical camera. It never sends
an account or user ID; the backend derives ownership from the revocable camera
credential.

The production API is `https://foodlog-api-sptvo5nsga-ew.a.run.app`. The persistent
test camera is `8ddd5941-6a92-4c3a-bac4-2a259cbba50c`; its credential is stored only
in gopass at `projects/gemini-foodlog/test-device/credential`.

Install the locked fixture client:

```sh
cd clients/python
uv sync --frozen
```

Verify the deployed camera credential without exposing it in shell history:

```sh
FOODLOG_CAMERA_CREDENTIAL="$(gopass show -o projects/gemini-foodlog/test-device/credential)" \
  uv run foodlog-camera \
  --camera-id 8ddd5941-6a92-4c3a-bac4-2a259cbba50c \
  status
```

Replay the reviewed distant-camera fixture:

```sh
FOODLOG_CAMERA_CREDENTIAL="$(gopass show -o projects/gemini-foodlog/test-device/credential)" \
  uv run foodlog-camera \
  --camera-id 8ddd5941-6a92-4c3a-bac4-2a259cbba50c \
  fixture ../../tests/fixtures/images/adversarial/synthetic-distant-ambiguous-meat-pack.png
```

Webcam mode is an explicit optional install because OpenCV is large:

```sh
uv sync --frozen --extra webcam
FOODLOG_CAMERA_CREDENTIAL="$(gopass show -o projects/gemini-foodlog/test-device/credential)" \
  uv run foodlog-camera \
  --camera-id 8ddd5941-6a92-4c3a-bac4-2a259cbba50c \
  webcam --count 5 --interval-seconds 1
```

Every frame receives one idempotency key that is preserved across bounded transient
retries. Revoked credentials, exhausted quota, and other permanent failures stop
immediately. The credential is accepted only through the environment or a local
credential file, never as a command-line value, and is redacted from client
representations and errors.
