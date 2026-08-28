# Synthetic distant ambiguous meat package v2 provenance

- Generated on 2026-08-28 with the built-in OpenAI still-image tool; Veo was not used.
- Edit source: `synthetic-distant-ambiguous-meat-pack.png`.
- Final SHA-256: `de2366772dea61d97eba571b9c84b82403d78bb77bbf0e3157bc85fbac4145f6`.
- Purpose: adversarial synthetic input where a fixed kitchen camera cannot safely identify the protein class from the image alone.

## First edit prompt

```text
Use case: precise-object-edit
Asset type: adversarial computer-vision test fixture for a kitchen camera
Primary request: make this genuinely ambiguous, poor-quality unattended-camera frame while preserving the same ordinary action of a person opening an unlabeled meat package beside an air-fryer basket near the sink.
Composition/framing: fixed side-view camera roughly 2 metres away; person and package substantially smaller in frame; package partly occluded by hands and counter edge; awkward angle; mild motion blur; cheap older-phone/webcam softness and compression noise.
Lighting/mood: dim uneven household lighting with slight color cast, so the meat's apparent color is not reliable enough to distinguish white meat from red meat.
Constraints: keep it photorealistic and candid; keep sink and air-fryer basket visibly nearby; no readable packaging label; no logos; no text; no watermark; do not make the food grotesque; do not make the meat clearly identifiable as chicken, pork, beef, or another specific protein.
```

## Second edit prompt

```text
Use case: precise-object-edit
Asset type: adversarial computer-vision test fixture for a kitchen camera
Primary request: make only the package and meat materially harder to identify. The package must be mostly hidden behind both moving hands and the counter/air-fryer basket, with only a tiny blurred glimpse of contents.
Lighting/mood: stronger mixed warm-and-green dim household color cast plus underexposure and compression artifacts over the food area, so pixels do not reliably reveal whether the contents are pale meat or red meat.
Constraints: preserve the current wide 2-metre camera placement, person, sink, air-fryer basket, kitchen layout, candid photorealism, and low-quality older-camera appearance; no label; no text; no watermark; do not make any protein identifiable; do not improve sharpness.
```
