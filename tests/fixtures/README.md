# Synthetic image fixtures

These privacy-safe still images were generated with the built-in OpenAI image-generation tool on 2026-08-25. They were **not** generated with Veo. They are synthetic regression inputs, not evidence of real-world accuracy.

## Ground truth

| File | Intended event | Required visible facts | SHA-256 |
| --- | --- | --- | --- |
| `images/synthetic-steak-airfryer.png` | Raw steak being prepared for air frying | Raw red beef steak; black air-fryer basket; basket beside a kitchen sink | `8e51f9691aebf6335d6d1cf1c7863b73654918bb97db3bd99bd5235e290da208` |
| `images/synthetic-chicken-airfryer.png` | Raw chicken being prepared for air frying | Two raw pale chicken breasts; black air-fryer basket; basket beside a kitchen sink | `7cf7f1c875e74bc5badeb79d1fbfc6656bed6fb782f6b1766c2772dca434cd97` |
| `images/synthetic-leftover-pasta.png` | Cooked tomato pasta being portioned for reheating | Cooked tomato fusilli; glass leftovers container; serving bowl; microwave context | `3fbc475d0d68302aed46aa5af483ba50367165918e8aa988eeb857bf55ecd848` |

## Prompt set

All three prompts requested a candid, photorealistic, wide consumer-webcam frame in an ordinary European kitchen, with natural imperfections and no people, faces, hands, logos, labels, readable text, or watermark.

- **Steak:** a clearly raw red beef steak inside an open black air-fryer basket beside a stainless-steel sink under ordinary warm evening light; explicitly excluded chicken, pork, cooked meat, and polished food styling.
- **Chicken:** two clearly raw pale chicken-breast fillets inside an open black air-fryer basket beside a stainless-steel sink under ordinary warm evening light; explicitly excluded red meat, steak, pork, cooked food, and polished styling.
- **Leftovers:** an open glass container of cooked tomato pasta with a serving transferred to a plain bowl and a microwave visible nearby; explicitly excluded raw meat, elaborate plating, and dramatic lighting.

## Evaluation rules

- Keep these files immutable; add a new version instead of overwriting one.
- Validate the recorded SHA-256 values before using them as release evidence.
- Never mix synthetic observations into a real household's learned knowledge.
- A model may reasonably mention additional visible objects, but it must not contradict the required facts above.
- These three independent frames do not test temporal event grouping or multi-camera correlation.
