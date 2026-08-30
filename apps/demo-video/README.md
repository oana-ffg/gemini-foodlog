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
   direction, then regenerates only changed narration. It reads
   `openai/api-key` directly from gopass and never writes the key to disk or logs.
4. Run `npm run demo:studio` for the interactive editor or
   `npm run demo:render` for the private MP4.

OpenAI's current speech guidance requires disclosure that the narrator is an
AI-generated voice. The final end card includes that disclosure.

## Private assets

`public/generated/` and `artifacts/demo-video/` are Git-ignored. The screenshot
sources are the deployed synthetic judge account captures documented in
`docs/demo-video-results.md`; they do not contain Oana's private household data.
The optional clip source names are:

- `artifacts/demo-video/veo/intro-cooking.mp4`
- `artifacts/demo-video/veo/intro-chaos.mp4`

If a clip is absent, the composition renders an intentional still-image fallback
instead of failing or fabricating a generated clip.
