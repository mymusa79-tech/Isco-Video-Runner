# Clean V2 bootstrap runbook

Clean V2 has one objective: keep the minimal production path stable while adding quality
layers one at a time. The first and only added layer is the existing automated Final Master
QC; Gold, Text Audit, Viewer Quality, and M7-M11 remain disabled.

## Runtime path

`Brief → Planning → Script → Voice → Visuals → Render → Final file → Final Master QC`

- Brief: exact human-approved Engine brief, bound by SHA-256.
- Planning: one bounded provider route.
- Script: one bounded provider route.
- Voice: verified local Piper voice, chunked deterministically.
- Visuals: Pexels, then Pixabay, then a deterministic local fallback if stock is unavailable.
- Render: FFmpeg H.264/AAC at 1920×1080 for Film or 1080×1920 for Moment/Story,
  with 48 kHz AAC so the unchanged legacy Final Master QC media contract can be reused.
- Final file: the original structural ffprobe check (video stream, audio stream, duration, size).
- Final Master QC: the existing `scripts/final_master_qc.py` core, unchanged. It performs
  stream/format checks plus full FFmpeg decode and automated black/silence/freeze detection.
  It adds zero AI calls and never mutates `final.mp4`.

There is no Gold, Text Audit, Viewer Quality, M7-M11, thumbnail, sibling Short, release,
or publication stage.

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
- `quality-final.json` and `visual-timeline.json` compatibility evidence for the unchanged QC core
- `final-master-qc.json`

## First-success definition

A run is successful only when all eight stages are `pass`, the structural final-file check
passes, and `final-master-qc.json` is `pass`. If Final Master QC blocks, the workflow fails
closed but preserves `final.mp4` and all earlier evidence; `run-manifest.json` records
`status=quality_pending` and `quality_pending_stage=final_master_qc`.

## Final Master QC stability cohort

After this change is merged:

1. Freeze the V2 Runner SHA, Engine SHA, brief hash, provider order, models, voice model,
   maximum visuals, render settings, and Final Master QC implementation.
2. Dispatch five new runs with the same approved topic and no code/configuration changes.
3. Measure the five-run production success rate with Final Master QC enabled.
4. Preserve failed-run artifacts so any QC block can be separated from planning/provider failures.
5. Do not add a second quality layer until this cohort is reviewed.

No workflow in Clean V2 publishes to YouTube or merges code automatically.
