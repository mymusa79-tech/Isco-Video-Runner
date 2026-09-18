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

Final Master QC is accepted as the new documented Clean V2 baseline because the
cohort completed at **80%**, above the prior **67%** baseline.

The fifth-attempt failure was classified as **infrastructure/content-provider
exhaustion before the new quality layer**, not as a Final Master QC block. It is a
known provider-availability failure family and is intentionally outside the scope
of the quality-layer restoration work. No code change is authorized for that
failure as part of this cohort.

## Next controlled layer

The next isolated experiment is the already-tested legacy
**Security V1 + Cinematic V2 (M7–M11)** stack.

Rules for that experiment:

1. Restore/reuse the existing tested implementation; do not rewrite the cinematic
   or security kernels.
2. One isolated branch and one separate PR.
3. Keep the same topic, Engine pin, provider order, media order, and Approved Brief.
4. Do not merge the layer PR before review.
5. After merge approval, run exactly five consecutive production attempts.
6. Classify every failed attempt as exactly one of:
   - `pre-layer`
   - `new-layer-block`
   - `infrastructure`
7. Do not treat known content-provider exhaustion as a reason to modify the new
   quality layer.
