# Production Closure Register

This document is the human-readable view only. The machine-readable authority is `scripts/production_family_closure.json`, and a family is considered closed for production only when `Verify Production Stage Ladder` certifies P0–P6 on the exact current Runner SHA and exact Engine pin.

## Closure 1 — Provider guidance / Retry-After ownership

**State:** ARCHITECTURALLY CLOSED; current validity is re-certified by P1.

Provider `Retry-After` is a minimum safe delay. One retry owner decides whether the full provider delay fits the local budget; otherwise the request fails over immediately. Partial sleep followed by an early same-provider retry is forbidden. Model-scoped short-window capacity and hard quota/session exhaustion remain separate failure classes.

## Closure 2 — Test / production source isolation

**State:** ARCHITECTURALLY CLOSED after Run129; current validity is re-certified by P0/P4/P5/P6.

Production input identity comes from the exact pinned Engine commit object. Approved-brief runtime bytes are materialized as a read-only snapshot. Test histories live under runner-temporary paths, tracked Engine source cleanliness is certified after test phases, and Production V4 fails closed on source mutation. Reset/restore/allowlist cleanup is not accepted as a substitute for isolation.

## Closure 3 — Runtime phase / ambient CI authority

**State:** ARCHITECTURALLY CLOSED by PR #366; current validity is re-certified by P0.

Run130 showed that workflow identity is not the same thing as live production phase. `scripts/runtime_phase.py` is the application-owned authority and requires explicit `ISCO_CANONICAL_RUNTIME=1` activation in addition to exact Production V4 identity. The historical planning-checkpoint implementation is retained only as compatibility/storage logic behind a wrapper that injects this authority before exporting any function.

PR #366 was merged only after Full Engine + Full Runner + M7 + M11 were Green on exact head `984983afa25f6898bcc7279fbf5e6c17006381cf`.

## Closure 4 — Run51–130 cumulative regression / stale-stage evidence

**State:** BLOCKING until the exact current `main` SHA receives a Green P0–P6 Stage Ladder certificate.

Run50 is the last independently verified historical End-to-End success and is the known-good media baseline. Its `final.mp4` identity is locked in the machine-readable register by exact size and SHA256. A historical fix, a test named after a run, or an old success at Stage 6 is not current closure evidence.

The mandatory ladder is:
- P0 — runtime, environment, state, checkpoint and test/production isolation.
- P1 — planning, schema/repair, provider routing, capacity and retry ownership.
- P2 — TTS, voice, audio semantics and mastering.
- P3 — media/visual feasibility, security and M7–M11 cinematic bindings.
- P4 — current Final Master QC over the real immutable `video-50/final.mp4` baseline.
- P5 — current Gold same-render state transition over that staging media with deterministic recorded external boundaries.
- P6 — current packaging, unified delivery and release-transaction dry replay with zero publication.

Every Run from 51 through 130 belongs to exactly one audit cohort in `scripts/production_family_closure.json`; known repeated failure families are mapped separately to executable contracts and required phases.

## Permanent production rule

A Green ladder run on a PR is review evidence only. After merge, the ladder must run again on the resulting `main` SHA. Only a successful **push-to-main** ladder may publish `stage-ladder-green-<sha>`, and Production V4's existing environment preflight must resolve that exact ref to `GITHUB_SHA` before provider secrets are materialized.

Therefore a commit after certification invalidates production readiness automatically. The Ladder never dispatches Production V4 and never publishes a release. Production remains manual.
