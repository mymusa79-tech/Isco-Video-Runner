# Clean V2 stability baselines

Date: 2026-09-18

This receipt records the production stability baselines used for staged Clean V2
quality-layer reintroduction. It is documentation only; it does not change runtime
code, provider routing, quality thresholds, or retry behavior.

## Baseline A — minimal Clean V2

- Cohort result: **4/6 successful = 67%**
- Runtime: minimal Clean V2 end-to-end path before Final Master QC was promoted as
  the first quality layer.
- Purpose: prove the simplified production path could complete repeatedly before
  restoring legacy quality/cinematic layers.

## Baseline B — Final Master QC

- Cohort result: **4/5 successful = 80%**
- Comparison baseline: **4/6 = 67%**
- Runner base after the Final Master compatibility fix:
  `14910d5fbcb5fc10ce4f98f8f1dc3d08618fc3e8`
- Engine pin:
  `3cbd689819e6b0e0b2ea9904d1998e24a5e2a293`
- Approved topic held constant:
  `لماذا تفشل خطط إدارة الوقت في الحياة اليومية`
- Provider order held constant: Gemini → Groq → OpenRouter
- Media order held constant: Pexels → Pixabay → deterministic local fallback
- Added quality layer: existing `scripts/final_master_qc.py` core, unchanged.

### Cohort interpretation

Final Master QC is accepted as the documented Clean V2 baseline because the cohort
completed at **80%**, above the prior **67%** baseline.

The fifth-attempt failure was classified as **infrastructure/content-provider
exhaustion before the new quality layer**, not as a Final Master QC block. It is a
known provider-availability failure family and is intentionally outside the scope
of the quality-layer restoration work.

## Baseline C — Security V1 + Cinematic V2 (M7–M11)

- Accepted cohort result after the compatibility fix: **5/5 successful = 100%**
- Comparison baseline: **4/5 = 80%**
- Frozen Runner SHA:
  `4383c70fd770f5face0df96114342450b7146de1`
- Engine pin:
  `3cbd689819e6b0e0b2ea9904d1998e24a5e2a293`
- Approved Brief SHA-256:
  `bcf8d3017ee8e18ee4808614c7c07731b0452a5ba6182e1183404f663078e129`
- Approved topic held constant:
  `لماذا تفشل خطط إدارة الوقت في الحياة اليومية`
- Provider order held constant: Gemini → Groq → OpenRouter
- Media order held constant: Pexels → Pixabay → deterministic local fallback
- Existing accepted layer retained: Final Master QC.
- Added layer: Security V1 + Cinematic V2 compatibility path using the restored
  M7–M11 owners.

### Pre-fix cohort evidence

The first strict five-run cohort on Runner
`7d9a9e3cfeea1cb1dd5b2138c16f6df4d52781d3` completed **0/5**:

- 3 attempts: `new-layer-block`
- 2 attempts: `infrastructure`

All three attempts that reached Security V1 blocked on the same integration defect:
Clean V2 produced normal English stock-search phrases containing comma separators
(and one non-breaking hyphen), while the restored Security V1 boundary correctly
required its stricter plain-search syntax.

PR #670 fixed only that compatibility seam. Security V1 itself, Engine schemas,
provider order, thresholds, retry behavior, and media order were not weakened or
changed.

### Accepted post-fix cohort

A fresh five-attempt cohort was then executed automatically but serially
(`max-parallel: 1`) so the attempts did not compete with one another. Every attempt
checked out the exact Runner SHA above, the exact Engine pin above, the same Approved
Brief, topic, provider/media order, Piper voice identity, render path, Security V1 +
M7–M11 layer, and Final Master QC.

Result:

1. attempt 1 — **success**
2. attempt 2 — **success**
3. attempt 3 — **success**
4. attempt 4 — **success**
5. attempt 5 — **success**

Therefore **5/5 = 100%** is the accepted Clean V2 stability baseline for
**Final Master QC + Security V1 + Cinematic V2 (M7–M11)**.

The temporary automation/verifier PR #671 used only to execute this cohort was closed
without merge after the evidence was collected.

## Next controlled layer — Gold only

The next experiment is **Gold evaluation only** on the already accepted long-form
artifact.

Sequencing rationale:

- Gold belongs to the preserved long-form quality stack.
- Sibling Shorts are a secondary deliverable and remain disabled until the long-form
  quality path is stable.
- Viewer Quality, packaging/release authority, thumbnails, Text Audit, Final Critic
  side layers, and sibling Shorts must not be activated as part of this experiment.

Rules for the Gold experiment:

1. One isolated branch and one separate PR.
2. Reuse the existing tested Gold critic implementation; do not create a new quality
   algorithm.
3. Keep Runner/Engine/Approved Brief/topic/provider order/media order/voice/render
   settings fixed.
4. Gold must evaluate the exact accepted `final.mp4` bytes and must not mutate them.
5. Do not enable Viewer Quality, packaging, publishing, thumbnails, Shorts, or another
   quality layer in the same PR.
6. Do not merge before review and regression checks.
7. After merge approval, run exactly five consecutive production attempts.
8. Classify each failure as exactly:
   - `pre-layer`
   - `new-layer-block`
   - `infrastructure`
9. Do not add the next layer until the five-run Gold cohort is reviewed.
