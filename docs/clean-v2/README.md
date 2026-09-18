# Clean V2 bootstrap runbook

Clean V2 has one objective: produce one complete `final.mp4`, then repeat the same path
successfully four more times before adding any quality layer.

## Runtime path

`Brief → Planning → Script → Voice → Visuals → Render → Final file`

- Brief: exact human-approved Engine brief, bound by SHA-256.
- Planning: one bounded provider route.
- Script: one bounded provider route.
- Voice: verified local Piper voice, chunked deterministically.
- Visuals: Pexels, then Pixabay, then a deterministic local fallback if stock is unavailable.
- Render: FFmpeg H.264/AAC at 1280×720 for Film or 720×1280 for Moment/Story.
- Final file: structural ffprobe check only (video stream, audio stream, duration, size).

There is no Gold, Text Audit, Final Master, Viewer Quality, thumbnail, sibling Short,
release, or publication stage.

## Manual production

Use the `Clean V2 Minimal E2E` GitHub Actions workflow after its branch is merged. It
always reads the frozen approved brief from Engine pin
`3cbd689819e6b0e0b2ea9904d1998e24a5e2a293`. It never accepts an unapproved topic input.

The artifact contains:

- `final.mp4`
- `run-manifest.json`
- `brief.json`, `plan.json`, and `script.json`
- `narration.txt` and `narration.wav`
- `provider-events.json` and `visual-events.json`
- `rights-manifest.json`
- `final.json`

## First-success definition

A run is successful only when all seven stages are `pass`, `final.mp4` contains at least
one video stream and one audio stream, duration is greater than one second, and the final
file hash is recorded. This is a completion contract, not a claim of Gold-level quality.

## Four-run stability confirmation

After the first successful production run:

1. Freeze the V2 Runner SHA, Engine SHA, brief hash, provider order, models, voice model,
   maximum visuals, and render settings.
2. Dispatch four more runs without code or configuration changes.
3. Count only consecutive successful runs. A failure resets the confirmation count.
4. Record all five Actions run IDs and final hashes in a later stability receipt.
5. Only after five consecutive successes may a proposal add one quality layer.

No workflow in Clean V2 publishes to YouTube or merges code automatically.
