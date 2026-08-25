# Synthetic image fixtures

These privacy-safe still images were generated with the built-in OpenAI image-generation tool on 2026-08-25. They were **not** generated with Veo. They are synthetic regression inputs, not evidence of real-world accuracy.

## Ground truth

| File | Intended event | Required visible facts | SHA-256 |
| --- | --- | --- | --- |
| `images/synthetic-steak-airfryer.png` | Raw steak being prepared for air frying | Raw red beef steak; black air-fryer basket; basket beside a kitchen sink | `8e51f9691aebf6335d6d1cf1c7863b73654918bb97db3bd99bd5235e290da208` |
| `images/synthetic-chicken-airfryer.png` | Raw chicken being prepared for air frying | Two raw pale chicken breasts; black air-fryer basket; basket beside a kitchen sink | `7cf7f1c875e74bc5badeb79d1fbfc6656bed6fb782f6b1766c2772dca434cd97` |
| `images/synthetic-leftover-pasta.png` | Cooked tomato pasta being portioned for reheating | Cooked tomato fusilli; glass leftovers container; serving bowl; microwave context | `3fbc475d0d68302aed46aa5af483ba50367165918e8aa988eeb857bf55ecd848` |

## Adversarial camera fixtures

These are deliberately difficult, out-of-distribution inputs. They are not registered in the deterministic local inference map: local and preview modes must accept them, mark them uncertain, and create a clarification question without calling a model.

| File | Intended ambiguity | SHA-256 |
| --- | --- | --- |
| `images/adversarial/synthetic-distant-red-meat-pack.png` | Side view from about 2 m; a person opens a pack beside the sink; basket nearby; dark-red meat is barely visible through hands, glare, blur, noise, and mixed lighting | `273a2e11050a5e7cc3464e6a45f039b8732ac0405244ada956c12e16c0fff906` |
| `images/adversarial/synthetic-distant-pale-meat-pack.png` | Awkward high corner view; pale fillets are partly hidden by hands and film; backlight and a green cast make poultry likely but not certain | `64d25cdef45a5b23c308c76bd582a4bde76bee957014b063d25af8d6189de369` |
| `images/adversarial/synthetic-distant-ambiguous-meat-pack.png` | Crooked side view; only a small pinkish glimpse is visible; the image intentionally preserves genuine poultry-versus-red-meat ambiguity | `9bc101311ae39bac06d36bd0df498735087c4c8ddce701c2f9a49dc0320164e6` |

## Prompt set

The three deterministic prompts requested a candid, photorealistic, wide consumer-webcam frame in an ordinary European kitchen, with natural imperfections and no people, faces, hands, logos, labels, readable text, or watermark. The three adversarial prompts intentionally include a cropped person and hands performing the ordinary package-opening action; faces remain out of frame and packaging has no readable label.

- **Steak:** a clearly raw red beef steak inside an open black air-fryer basket beside a stainless-steel sink under ordinary warm evening light; explicitly excluded chicken, pork, cooked meat, and polished food styling.
- **Chicken:** two clearly raw pale chicken-breast fillets inside an open black air-fryer basket beside a stainless-steel sink under ordinary warm evening light; explicitly excluded red meat, steak, pork, cooked food, and polished styling.
- **Leftovers:** an open glass container of cooked tomato pasta with a serving transferred to a plain bowl and a microwave visible nearby; explicitly excluded raw meat, elaborate plating, and dramatic lighting.
- **Distant red:** a cheap fixed camera about 2 m away, side view of a person opening a generic meat pack by the sink with an air-fryer basket nearby; dark-red meat barely distinguishable through hands, glare, blur, noise, and poor mixed lighting.
- **Distant pale:** an awkward high-corner webcam view of a person opening a tray near the sink; pale fillets partly obscured by hands and film, with backlight, greenish white balance, rolling-shutter smear, and an out-of-focus basket.
- **Distant ambiguous:** a crooked, underexposed side view with the package near the frame edge; only a narrow pinkish glimpse that could plausibly be poultry or red meat, with background focus, motion blur, sensor grain, and lens haze.

## Evaluation rules

- Keep these files immutable; add a new version instead of overwriting one.
- Validate the recorded SHA-256 values before using them as release evidence.
- Never mix synthetic observations into a real household's learned knowledge.
- A model may reasonably mention additional visible objects, but it must not contradict the required facts above.
- These six independent frames do not test temporal event grouping or multi-camera correlation.
