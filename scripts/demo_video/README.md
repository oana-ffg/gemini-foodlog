# Private demo-video builder

This directory builds the private `REL-010` review draft from production-backed
screenshots. It never signs in, calls an API, changes FoodLog data, or publishes
media. Browser capture and publication remain separate approval-gated steps.

## Required private inputs

Create one local directory containing these 1920-by-1080-or-larger PNGs from the
dedicated synthetic judge account and current production release:

- `01_timeline.png` — hosted journal overview;
- `02_ambiguous_detail.png` — reviewed tentative result, evidence, alternatives,
  and synthetic context;
- `03_correction_history.png` — correction plus immutable revision history;
- `04_knowledge.png` — scoped household-wiki revision;
- `05_cat_discarded.png` — retained cat evidence under discarded activity;
- `06_patterns.png` — longitudinal pattern hypothesis with evidence;
- `07_cloud_proof.png` — sanitized read-only Cloud Run/Vertex proof from the same
  release, with no account, token, object, or trace identifier.

The builder rejects missing or undersized shots. Do not use Oana's private
household account or real kitchen images. `docs/demo-runbook.md` remains the
content and claim authority.

## Build

The local Mac needs Python 3 with Pillow, Graphviz `dot`, `ffmpeg`, `ffprobe`, and
the built-in `say` command. Generated media belongs outside Git; the repository
ignores `artifacts/demo-video/`.

```bash
python3 scripts/demo_video/build.py \
  --shots-dir /absolute/path/to/reviewed-shots \
  --output artifacts/demo-video/foodlog-demo-private-draft.mp4
```

The script renders the architecture from the canonical Mermaid source, uses the
reviewed synthetic ambiguous-meat fixture only for the opening card, generates
local English narration, assembles a 1920-by-1080 H.264/AAC video, and fails if
the result exceeds four minutes. It emits the duration, byte count, and SHA-256
for the privacy review record.

`render_cloud_proof.py` turns a local JSON readback into `07_cloud_proof.png`.
It requires an immutable digest, 100-percent traffic, and Vertex AI enabled, so
the card cannot silently render incomplete or mutable deployment evidence. Keep
that JSON and rendered PNG in the ignored private-shots directory, not Git.
