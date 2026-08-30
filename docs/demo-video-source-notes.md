# Demo-video factual and tool source notes

These notes support the public narration without turning the demo into a medical
claim. The product records evidence and helps a person inspect patterns; it does
not diagnose a condition or establish that a food caused a symptom.

## Opening health statement

The narration says:

> A food and symptom journal can help people spot patterns around migraine,
> eczema, and other symptoms.

Supporting sources:

- [NHS migraine guidance](https://www.nhs.uk/conditions/migraine/) says a
  migraine diary can help a person work out possible triggers.
- [National Eczema Society, Exchange Summer 2024](https://eczema.org/wp-content/uploads/NES-Exchange-Summer-2024.pdf)
  recommends mapping a food diary to symptoms, ideally with a healthcare
  professional or dietitian, when investigating whether foods may worsen eczema
  symptoms.

The video deliberately says “spot patterns,” not “identify the cause,” and the
end card states that FoodLog does not diagnose conditions or establish
causality.

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

## Remotion

- [Remotion documentation](https://www.remotion.dev/docs/) documents React-based
  programmatic MP4 creation and local Studio editing.
- This project pins Remotion `4.0.518`. The private first pass rendered ten
  modular scenes at 1920×1080, 30 fps, and three minutes; the final verification
  gate rejects a duration over four minutes, black gaps, the wrong codecs or
  dimensions, and audio silences of at least five seconds.
