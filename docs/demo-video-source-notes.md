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

## OpenAI narration

- [OpenAI text-to-speech guide](https://developers.openai.com/api/docs/guides/text-to-speech)
  identifies `gpt-4o-mini-tts` as its newest reliable speech model, supports
  instructions for emotional range, intonation, speed, and tone, and recommends
  `marin` or `cedar` for best quality.
- The same guide requires clear disclosure that the listener is hearing an
  AI-generated rather than human voice. The FoodLog end card includes this
  disclosure.
- The project pins snapshot `gpt-4o-mini-tts-2025-12-15`, uses the `marin` voice,
  and stores each scene separately under a content hash so unchanged narration
  is reused.

The ChatGPT subscription does not fund API requests. The existing gopass key is
currently valid but its organization returned `credit_balance_exhausted`; no
audio was generated and no TTS cost was incurred by that failed request.

## Remotion

- [Remotion documentation](https://www.remotion.dev/docs/) documents React-based
  programmatic MP4 creation and local Studio editing.
- This project pins Remotion `4.0.518`. The private first pass rendered ten
  modular scenes at 1920×1080, 30 fps, and three minutes; the final verification
  gate rejects a duration over four minutes, black gaps, the wrong codecs or
  dimensions, and audio silences of at least five seconds.
