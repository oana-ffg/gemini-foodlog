# Private demonstration video evidence

Recorded: 30 August 2026

## Result

The reproducible Remotion project produced a private English demonstration cut
from the deployed FoodLog UI, Gemini narration, and two bounded Veo shots:

- output: `artifacts/demo-video/foodlog-demo-remotion.mp4` (ignored by Git)
- duration: 191.786667 seconds (3 minutes 11.787 seconds)
- size: 42,054,893 bytes
- SHA-256: `0a9894dfb3bbff40f93ecad94fb9461534c7bd33b36027bb71afb9e6b417095d`
- video: H.264 High profile, 1920 by 1080, 30 frames per second
- audio: AAC LC, 48 kHz, stereo

The ten-scene cut covers the human problem, passive-camera value, authenticated
Cloud Run architecture, a real production Gemini result with useful
uncertainty, correction history, household-scoped learning, a discarded cat
negative control, a longitudinal pattern hypothesis, and sanitized live Google
Cloud deployment evidence. It is 48.213 seconds below the four-minute limit.

## Source and privacy boundary

The UI captures came from the deployed production application while signed into the dedicated synthetic judge account. They contain synthetic fixtures and synthetic judge history only; no private household account, email address, credential, storage path, trace identifier, or object identifier appears in the recorded frames.

The Google Cloud card is a sanitized read-only capture of the deployed `foodlog-inference` service at revision `foodlog-inference-00048-w5z`, 100 percent traffic, immutable container digest `sha256:60c08e90aa3092ac1805afc8d3a1c7fa200441f056ffe0a2ac1330c7201e92fe`, Gemini 3.6 Flash through Vertex AI, one vCPU, one GiB, and the DKK 400 application model hard cap. The container digest is release evidence, not a secret.

The seven UI screenshots still come only from the synthetic judge account. The
opening cinematic shots are synthetic Veo outputs and contain no private user
data. Narration uses Gemini 3.1 Flash TTS Preview with the `Sulafat` voice. The
private media, extracted verification frames, and contact sheets remain local
and ignored by Git.

The ten TTS calls cost DKK 0.573402 gross list price. The two explicitly
authorized Veo calls cost DKK 3.081600 gross list price. Both are recorded in
`docs/testing-spend-ledger.md`; promotional-credit settlement remains pending
Cloud Billing.

## Automated verification

- all seven source captures are at least 1920 by 1080
- both Veo files are exactly eight seconds, 1280 by 720, H.264, and 24 fps
- all ten Gemini WAV files are 24 kHz mono PCM and content-addressed
- OCR privacy-pattern scan found no judge email, account identifier, user identifier, trace identifier, or long opaque identifier; it found only the deliberately displayed release container digest
- `ffprobe` confirmed the expected H.264/AAC streams, dimensions, frame rate, sample rate, channel count, duration, and byte size
- the assembled-file SHA-256 independently matched the verifier report
- ten evenly spaced extracted frames were visually inspected together and
  showed readable intended content with no blank segment
- `silencedetect` found no audio gap of five seconds or longer
- the verifier found no black interval of at least half a second and rejects a
  complete video over four minutes
- the branded Firebase Hosting release returned the new asset hashes, and its
  live logo bytes exactly matched the canonical SVG

## Remaining human gate

This is a private cut, not publication authorization. Oana still needs to watch
it with sound and complete REL-011's frame-by-frame privacy and intellectual-
property review. Any requested edit must be rerendered and reverified before a
public upload.
