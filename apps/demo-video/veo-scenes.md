# Opening Veo scene specifications

These two slots are optional enhancements to the Remotion composition. They
remain still-image fallbacks until Oana explicitly approves the exact bounded
generation cost. Generate video only: no dialogue, music, or sound effects.

## `intro-cooking.mp4`

**Purpose:** Support the opening line about food-and-symptom journals without
making the person stage ingredients or photograph a plate.

**Prompt:**

> Warm, emotionally grounded documentary footage in an ordinary lived-in home
> kitchen at dinner time. Wide side view from a fixed inexpensive camera around
> two metres away. An adult woman cooks naturally at the counter and stove,
> moving between vegetables, a pan, and a simple meal without posing for the
> camera or using a phone. Only natural continuous activity. Soft late-afternoon
> window light mixed with practical kitchen light, realistic textures, subtly
> cinematic but not glossy advertising. No readable labels, no logos, no text,
> no medical imagery, no identifiable private information, no dialogue, no
> audio. One continuous eight-second 16:9 shot.

**Acceptance:** an ordinary cooking action is obvious; nothing suggests calorie
tracking, staged photography, or luxury lifestyle advertising; no text or
anatomical artifacts.

## `intro-chaos.mp4`

**Purpose:** Support “when life is already chaotic … Mission impossible” with a
recognizable, funny beat that does not trivialize pain or show injury.

**Prompt:**

> Emotionally truthful but gently comedic documentary footage in a messy
> lived-in kitchen after dinner. Medium-wide side view. An adult woman sits at
> the table holding her aching temple with her left hand while trying to type a
> food log into her smartphone with her right hand. She looks exhausted and
> increasingly exasperated. In the final second she abruptly slams the phone
> face-down onto the wooden table in frustration. The phone remains intact: no
> broken glass, injury, self-harm, or dangerous debris. Natural practical light,
> realistic home texture, one continuous action, no melodramatic acting, no
> readable screen, no logos, no text, no dialogue, no audio. One continuous
> eight-second 16:9 shot.

**Acceptance:** left-hand temple hold and right-hand typing are both clear before
the final frustrated phone slam; the beat reads as relatable exasperation, not
violence, injury, or a migraine diagnosis; the phone screen contains no legible
content.

## Approved generation shape (pending explicit yes)

- Vertex AI model: `veo-3.1-lite-generate-001` in `us-central1`
- two clips, one generated video per request
- eight seconds each, 720p, 16:9, video-only
- adult person generation allowed
- SDK attempts set to one; no automatic resubmission
- gross list-price ceiling: DKK 3.081600 total at the recorded 6.42 USD/DKK rate
- stop after one attempt for each scene even if one is rejected or aesthetically
  weak; any retry needs a new exact approval
