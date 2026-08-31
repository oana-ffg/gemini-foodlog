# Lyria final-video score

One bounded Lyria 3 Pro generation supplies an optional instrumental bed for
the private Remotion cut. The source audio remains Git-ignored and is copied
into the Remotion public directory by `npm run demo:assets`.

## Exact prompt

> Create a 184-second instrumental underscore for a heartfelt technology
> documentary. No vocals, lyrics, spoken words, choir, humming, sound effects,
> or recognizable existing melody. The music must sit quietly beneath a warm
> woman narrator, with a spacious midrange and no attention-grabbing lead.
>
> [0:00-0:18] Intimate and empathetic: sparse felt piano, soft warm analogue
> pad, gentle room texture, around 76 BPM. Acknowledge that tracking food while
> unwell and overwhelmed is difficult, without becoming sad or medical.
>
> [0:18-0:35] A small hopeful lift as a solution appears: add a delicate muted
> pulse and subtle marimba-like organic plucks, still restrained.
>
> [0:35-1:22] Quiet forward motion for an engineering explanation: warm
> minimal electronic pulse, soft pizzicato details, clean and intelligent, no
> corporate triumph or trailer drama.
>
> [1:22-2:18] More human and reassuring for uncertainty, corrections, and
> learning: return the felt piano motif with gentle strings and organic
> texture. Keep narration completely clear.
>
> [2:18-2:48] Curious, lightly playful warmth for a cat negative control and
> patterns over time, never whimsical comedy.
>
> [2:48-3:04] Restrained emotional resolution: broaden the warm harmony,
> briefly reprise the piano motif, then decrescendo to a clean, complete ending
> by 3:04. Quiet confidence, not a commercial crescendo.
>
> Cohesive single composition, natural transitions, modern but timeless,
> emotionally sincere, low dynamic range suitable for dialogue mixing.

## Generation boundary

- Vertex AI model: `lyria-3-pro-preview`, global Interactions API
- exactly one request, SDK attempts set to one, no automatic resubmission
- maximum gross list price: USD 0.08 / DKK 0.513600 at USD/DKK 6.42
- any alternate or retry requires new approval

## Generated result

- duration: 179.774625 seconds
- format: MP3, 44.1 kHz, stereo, 192 kbps
- size: 4,322,259 bytes
- SHA-256: `8706bec04894d72a4b7ff07d0679bc826086c18fccf92cf4990debdd064313a9`
- private path: `artifacts/demo-video/lyria/foodlog-score.mp3`
- request count: one successful request; no retry
- human disposition: not accepted for the final cut; the first listening pass
  found the composition weird. The section-by-section instrumentation and mood
  changes are the leading explanation, so any future attempt should begin from
  a substantially simpler brief rather than iterating on this prompt.

## Follow-up comparison set

Oana authorized exactly three one-shot follow-up experiments. Their exact
prompts are source-controlled under `scripts/lyria-prompts/`:

- `uplifting-techno`: one restrained coherent groove, explicitly excluding club
  dynamics, drops, and sudden loudness changes
- `ai-product-ad`: polished ambient electronica for a credible AI product ad
- `human-tech-trailer`: one shared motif across a coherent multi-instrument
  human-meets-technology arrangement

All three prohibit vocals and prioritize narration. They are compared through
the same post-render mixer rather than embedded at a fixed level in Remotion.
The mixer lowers the music, removes competing vocal frequencies, sidechain-
ducks it under speech, extends it with a crossfade instead of a hard loop, and
masters every comparison identically.

## Human selection

On 31 August 2026, Oana selected `uplifting-techno`, the first comparison, as
the final soundtrack direction. The selected private comparison cut is
`artifacts/demo-video/lyria-comparisons/foodlog-demo-uplifting-techno.mp4` with
SHA-256 `0eded8981420b8739b31add7418a1d89477bb4e5727a904ff6fe4977c73ccd32`.
The remaining gate is the final frame-by-frame privacy, IP, wording, and pacing
review; selecting the score is not publication authorization.
