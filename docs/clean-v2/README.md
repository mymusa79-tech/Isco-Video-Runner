# Clean V2 bootstrap runbook

Clean V2 has one objective: keep the minimal production path stable while adding quality
layers one at a time. Final Master QC is the accepted first layer and Security V1 +
Cinematic V2 (M7-M11) is the accepted second layer. The third controlled layer is
Final-cut Visual QA over the exact clips already selected by Clean V2. Gold, Text Audit,
Viewer Quality, packaging, thumbnails, and Shorts remain disabled.

## Runtime path

`Brief → Planning → Script → Voice → Visuals[Security V1 + M8] → Final-cut Visual QA → Render → M7/M9/M10/M11 compatibility stage → Final file → Final Master QC`

- Brief: exact human-approved Engine brief, bound by SHA-256.
- Planning: one bounded provider route.
- Script: one bounded provider route.
- Voice: verified local Piper voice, chunked deterministically.
- Visuals: Pexels, then Pixabay, then a deterministic local fallback if stock is unavailable.
  Stock queries and downloaded stock cross the existing Security V1 boundaries. Admitted
  clips then pass through the existing Engine M8 BT.709/SDR kernel before render.
- Final-cut Visual QA: audits only the already-selected clips with the existing Engine
  visual normalizer, unchanged 0.85 final-cut semantic target, and bounded
  Gemini → Groq → OpenRouter Vision mesh. It cannot re-search or replace footage.
- Render: FFmpeg H.264/AAC at 1920×1080 for Film or 1080×1920 for Moment/Story,
  with 48 kHz AAC so the unchanged legacy Final Master QC media contract can be reused.
- M7-M11 compatibility: the existing Engine M7 legacy fallback compiles the already-selected
  Clean V2 assets without fabricating Director or Vision evidence; HEI metadata is bound,
  existing M9 policy evaluates transitions, M10 evidence-only Quote/Stat cards may render,
  and the exact M11 runtime is invoked with no invented Director scene plan (therefore it
  truthfully remains not-applicable unless that evidence exists in a future controlled layer).
- Final file: the original structural ffprobe check (video stream, audio stream, duration, size).
- Final Master QC: the existing `scripts/final_master_qc.py` core, unchanged. It performs
  stream/format checks plus full FFmpeg decode and automated black/silence/freeze detection.
  It adds zero AI calls and never mutates `final.mp4`.

There is no Gold, Text Audit, Viewer Quality, thumbnail, sibling Short, release, or
publication stage. This candidate adds no AI/provider call to the M7-M11 compatibility
stage and does not alter the provider order used by Planning/Script.

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
- `visual-timeline.json`, `m9-transitions.json`, `m10-cards.json`, and `m11-report.json`
- `security-cinematic-v2.json` and per-clip `visuals/*.m8.json`
- `visual-audit.json`, `visual-qa-report.json`, and `visual-qa-budget.json`
- `quality-final.json` compatibility evidence for the unchanged Final Master core
- `final-master-qc.json`

## First-success definition

A run is successful only when all ten stages are `pass`, the Final-cut Visual QA report is
`pass`, the Security/Cinematic report is
`pass`, the structural final-file check passes, and `final-master-qc.json` is `pass`.
A Final-cut Visual QA, Security/Cinematic, or Final Master block fails closed and records
`status=quality_pending`. Attribution stays explicit for this cohort: a semantic Visual
QA block is `new-layer-block`; failures from already-accepted Security/Cinematic or
Final Master layers are `pre-layer`; bounded provider-mesh exhaustion is
`infrastructure`.

## Second-layer stability cohort

The accepted Final Master cohort is 4/5 = 80%, versus the preceding 4/6 = 67% baseline.
After this second-layer PR is reviewed and merged:

1. Freeze the V2 Runner SHA, Engine SHA `3cbd689819e6b0e0b2ea9904d1998e24a5e2a293`,
   Approved Brief, provider/media order, voice, render settings, and layer implementation.
2. Use the same approved topic: `لماذا تفشل خطط إدارة الوقت في الحياة اليومية`.
3. Dispatch exactly five consecutive runs with no code/configuration changes.
4. Classify every failure as exactly `pre-layer`, `new-layer-block`, or `infrastructure`.
5. Known content-provider exhaustion remains an infrastructure/provider-availability event;
   it is not authorization to modify this layer.

No workflow in Clean V2 publishes to YouTube or merges code automatically.
