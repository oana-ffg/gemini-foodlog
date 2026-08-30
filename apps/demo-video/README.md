# FoodLog Remotion demo

This is the editable final-video project for `REL-010`. Narration, screenshots,
generated clips, timing, and visuals are deliberately separate so one scene can
change without regenerating the rest.

## Edit and preview

1. Edit `src/content.json`. Every scene owns its narration and voice direction.
2. Run `npm run demo:assets` from the repository root. This copies the already
   privacy-reviewed production screenshots and canonical logo into the ignored
   Remotion public directory. If approved Veo clips exist, it copies those too.
3. Run `npm run demo:tts`. The script hashes each scene's model, voice, text, and
   direction, then regenerates only changed narration. It uses ambient Google
   credentials with Vertex AI; no API key is stored in the project.
4. Run `npm run demo:studio` for the interactive editor or
   `npm run demo:render` for the private MP4.

Remotion always renders a narration-first master. Approved Lyria experiments
live under `artifacts/demo-video/lyria/experiments/`; their exact prompts live
under `scripts/lyria-prompts/`. Generate one named variant with
`generate_lyria.py --variant NAME`, then use `mix_lyria_variant.py --variant
NAME` to create a comparison without rerendering or regenerating narration. The
mixer keeps music low, removes competing vocal frequencies, ducks it while the
narrator speaks, and masters the combined audio consistently.

For a human-review comparison cycle, `npm run demo:render:corrected` preserves
the previous verified cut and produces the narration master expected by the
mixer.

The final end card clearly discloses that the narrator is an AI-generated
Gemini voice.

## Private assets

`public/generated/` and `artifacts/demo-video/` are Git-ignored. The screenshot
sources are the deployed synthetic judge account captures documented in
`docs/demo-video-results.md`; they do not contain Oana's private household data.
The optional clip source names are:

- `artifacts/demo-video/veo/intro-cooking.mp4`
- `artifacts/demo-video/veo/intro-chaos.mp4`

If a clip is absent, the composition renders an intentional still-image fallback
instead of failing or fabricating a generated clip.
