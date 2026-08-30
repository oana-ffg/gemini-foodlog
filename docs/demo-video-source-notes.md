# Demo-video factual and tool source notes

These notes support the public narration without turning the demo into a medical
claim. The product records evidence and helps a person inspect patterns; it does
not diagnose a condition or establish that a food caused a symptom.

## Opening health statement

The narration says:

> Keeping a detailed food journal can help people look for possible links
> between what they eat and symptoms such as migraines or eczema.

Supporting sources:

- [NHS migraine guidance](https://www.nhs.uk/conditions/migraine/) says a
  migraine diary can help a person work out possible triggers.
- [National Eczema Society, Exchange Summer 2024](https://eczema.org/wp-content/uploads/NES-Exchange-Summer-2024.pdf)
  recommends mapping a food diary to symptoms, ideally with a healthcare
  professional or dietitian, when investigating whether foods may worsen eczema
  symptoms.

The video deliberately says “possible links,” not “identify the cause,” and the
end card states that FoodLog does not diagnose conditions or establish
causality. The narration describes the value of the food record; the hackathon
MVP does not collect symptoms or perform food-and-symptom association analysis.

## Gemini narration

- [Gemini text-to-speech guidance](https://ai.google.dev/gemini-api/docs/speech-generation)
  documents steerable tone, pace, style, single-speaker voices, and the
  `gemini-3.1-flash-tts-preview` model used here.
- [Official Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
  lists standard paid-tier pricing at USD 1 per million input text tokens and
  USD 20 per million output audio tokens, with 25 audio tokens per second.
- The project uses the warm `Sulafat` voice through Vertex AI and stores each
  scene separately under a content hash so unchanged narration is reused.
- The FoodLog end card discloses that the narration is AI-generated.
- After the opening-line correction, the ten selected scene files total 176.840
  seconds, 1,917 input tokens, and 4,421 output audio tokens. Their selected-file
  gross list price is DKK 0.579964 at the recorded USD/DKK rate of 6.42. The
  superseded opening file was also billed; the complete request spend remains
  in `docs/testing-spend-ledger.md`. Promotional-credit settlement remains
  pending the Cloud Billing export.

## Veo opening shots

- Both opening shots use Vertex AI model `veo-3.1-lite-generate-001`, one
  eight-second 720p, 16:9, video-only request each, with SDK retries disabled.
- The two exact prompts, safety boundaries, and acceptance criteria are in
  `apps/demo-video/veo-scenes.md`.
- The two completed generations cost DKK 3.081600 gross list price in total.
  The second reads clearly as exhausted exasperation but uses a sharp phone
  put-down and collapse rather than the requested literal face-down slam; no
  retry was authorized or made.

## Lyria soundtrack

- [Google's Lyria 3 documentation](https://ai.google.dev/gemini-api/docs/music-generation)
  documents full-length Lyria 3 Pro generation, prompt-controlled structure,
  instrumental output, SynthID watermarking, and single-turn generation.
- One `lyria-3-pro-preview` request through Vertex AI produced a 179.774625-
  second, 44.1 kHz stereo MP3. Its exact timed prompt is preserved in
  `apps/demo-video/lyria-score.md`.
- The request cost DKK 0.513600 gross list price at the official USD 0.08 per
  successful generation and the recorded USD/DKK rate of 6.42. The Remotion
  mix keeps it at eight percent volume under narration, fades in for two
  seconds, and fades out over its final six seconds.

## Remotion

- [Remotion documentation](https://www.remotion.dev/docs/) documents React-based
  programmatic MP4 creation and local Studio editing.
- This project pins Remotion `4.0.518`. The private first pass rendered ten
  modular scenes at 1920×1080 and 30 fps. The complete narrated cut is 191.787
  seconds; the final verification gate rejects a duration over four minutes,
  black gaps, the wrong codecs or dimensions, and audio silences of at least
  five seconds.
