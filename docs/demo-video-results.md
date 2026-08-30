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

## Lyria comparison cut

One explicitly authorized `lyria-3-pro-preview` request produced an optional
179.774625-second instrumental score through the Vertex AI global Interactions
API. The exact timed prompt and generation boundary are preserved in
`apps/demo-video/lyria-score.md`; the private MP3 is Git-ignored and has SHA-256
`8706bec04894d72a4b7ff07d0679bc826086c18fccf92cf4990debdd064313a9`.

The score is mixed at eight percent beneath the narration with a two-second
fade-in and six-second fade-out. A comparison cut is available at
`artifacts/demo-video/foodlog-demo-remotion-lyria-preview-mastered.mp4`:

- duration: 191.8 seconds
- size: 41,148,457 bytes
- SHA-256: `dfebaf19b82f3334f85816183e04a53a9f1bad6eac0c4f0810b64ce60915bb75`
- video: unchanged H.264 source stream, 1920 by 1080, 30 frames per second
- audio: remixed AAC LC, 48 kHz, stereo, mastered to -15.8 LUFS and -1.5 dBTP

The comparison passed the same duration, stream, black-gap, and five-second
silence gates as the music-free cut. It still requires a human listening pass
for musical fit, narration intelligibility, and confirmation that the generated
audio contains no unwanted vocal-like content. The music-free original remains
untouched so the comparison is reversible.

## Pending opening-narration correction

Human review found that the original opening voice inserted a confusing prosodic
break after “around.” The selected source now accurately says that a detailed
food journal can help someone look for possible links between food and symptoms;
it no longer calls the MVP a food-and-symptom journal. One replacement
`health-patterns` file was generated with explicit continuous-flow direction:

- duration: 9.32 seconds
- format: 24 kHz mono PCM WAV
- SHA-256: `4000fec9dbf7e7a6b5c3edb1009d43d84573422463436379d0805a37d924ab1d`
- internal detected gaps: 0.1245 and 0.163 seconds

This isolated replacement needs Oana's listening approval before the full film
is rerendered. The currently verified MP4 files still contain the superseded
opening narration; their evidence above therefore remains accurate.

## Corrected master and three soundtrack comparisons

The corrected narration-first Remotion master preserves the earlier cut and
incorporates the revised opening:

- output: `artifacts/demo-video/foodlog-demo-remotion-corrected.mp4`
- duration: 193.749333 seconds
- size: 43,210,470 bytes
- SHA-256: `56a224a3d08f6c12f40725633d06927dbeba35de91881c5ef6b226804f72dcfb`

Three one-request Lyria experiments were mixed from that single master. To
prevent another loudness-biased comparison, every score receives the same
six-decibel-lower base level, five-decibel vocal-midrange notch, narration-led
sidechain ducking, crossfaded duration extension, final ten-second fade, and
mastering target:

| Variant | Comparison SHA-256 | Bytes | Integrated loudness | True peak |
| --- | --- | ---: | ---: | ---: |
| Uplifting techno | `0eded8981420b8739b31add7418a1d89477bb4e5727a904ff6fe4977c73ccd32` | 40,202,786 | -15.1 LUFS | -1.0 dBFS |
| AI product ad | `75bfacff909e3ef20021497317e58621652a06a36a2a637eadc2f5ffc4aa0a67` | 40,201,409 | -15.1 LUFS | -1.0 dBFS |
| Human-tech trailer | `7e43601d449390e80e306f2e8e83fcef3538df048d274f8ea7245a4a58c85a61` | 40,205,923 | -15.1 LUFS | -1.1 dBFS |

Each comparison is 193.8 seconds with unchanged H.264 1920-by-1080 30 fps
video and AAC 48 kHz stereo audio. All three passed duration, stream,
black-gap, five-second-silence, and independent-hash verification. Human review
must choose one, request a mix-only adjustment, or reject all three.

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
both versions with sound, choose the soundtrack treatment, and complete
REL-011's frame-by-frame privacy and intellectual-property review. Any requested
edit must be rerendered and reverified before a public upload.
