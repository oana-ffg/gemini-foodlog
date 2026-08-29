# Private demonstration video evidence

Recorded: 29 August 2026

## Result

The reproducible local builder produced a private English demonstration draft from the deployed FoodLog UI:

- output: `artifacts/demo-video/foodlog-demo-private-draft.mp4` (ignored by Git)
- duration: 198.033 seconds (3 minutes 18.033 seconds)
- size: 7,682,508 bytes
- SHA-256: `9b56b6da6b7f5f54f4b12478cf9c2b3ae568574b7ee66dda2f576c021497e982`
- video: H.264 High profile, 1920 by 1080, 30 frames per second
- audio: AAC LC, 48 kHz, stereo

The eight-segment draft covers the problem, value, authenticated Cloud Run architecture, a real production Gemini result with useful uncertainty, correction history, household-scoped learning, a discarded cat negative control, a longitudinal pattern hypothesis, and sanitized live Google Cloud deployment evidence. It is 41.967 seconds below the four-minute hackathon limit.

## Source and privacy boundary

The UI captures came from the deployed production application while signed into the dedicated synthetic judge account. They contain synthetic fixtures and synthetic judge history only; no private household account, email address, credential, storage path, trace identifier, or object identifier appears in the recorded frames.

The Google Cloud card is a sanitized read-only capture of the deployed `foodlog-inference` service at revision `foodlog-inference-00048-w5z`, 100 percent traffic, immutable container digest `sha256:60c08e90aa3092ac1805afc8d3a1c7fa200441f056ffe0a2ac1330c7201e92fe`, Gemini 3.6 Flash through Vertex AI, one vCPU, one GiB, and the DKK 400 application model hard cap. The container digest is release evidence, not a secret.

Capturing and rendering the draft made no account-data mutation, cloud write, model call, Veo generation, or paid-service request. The screenshot directory, rendered draft, extracted verification frames, and contact sheet remain local and ignored by Git.

## Automated verification

- all seven source captures are at least 1920 by 1080
- OCR privacy-pattern scan found no judge email, account identifier, user identifier, trace identifier, or long opaque identifier; it found only the deliberately displayed release container digest
- `ffprobe` confirmed the expected H.264/AAC streams, dimensions, frame rate, sample rate, channel count, duration, and byte size
- the assembled-file SHA-256 independently matched the builder report
- eight extracted frames, one from every segment, were visually inspected together and showed the intended readable content with no accidental blank segment
- `silencedetect` found no audio gap of five seconds or longer
- the builder now rejects any segment whose configured duration would create more than five seconds of trailing silence, and still rejects a complete video over four minutes

## Remaining human gate

This is a private draft, not publication authorization. Oana still needs to watch it with sound and complete REL-011's frame-by-frame privacy and intellectual-property review. Any requested edit must be rerendered and reverified before a public upload.
