# Production Closure Register

This document is the human-readable view only. The machine-readable authority is `scripts/production_family_closure.json`, and a family is considered closed for production only when `Verify Production Stage Ladder` certifies P0–P6 on the exact current Runner SHA and exact Engine pin **and every family contract declared by the register is actually executed by that ladder**.

## Closure 1 — Provider guidance / Retry-After ownership

**State:** ARCHITECTURALLY CLOSED; current validity is re-certified by P1.

Provider `Retry-After` is a minimum safe delay. One retry owner decides whether the full provider delay fits the local budget; otherwise the request fails over immediately. Partial sleep followed by an early same-provider retry is forbidden. Model-scoped short-window capacity and hard quota/session exhaustion remain separate failure classes.

## Closure 2 — Test / production source isolation

**State:** ARCHITECTURALLY CLOSED after Run129 and strengthened by Run131; current validity is re-certified by P0/P4/P5/P6.

Production input identity comes from the exact pinned Engine commit object. Approved-brief runtime bytes are materialized as a read-only snapshot. The Engine semantic approval fingerprint and the immutable snapshot raw-byte SHA256 are separate authorities. Test histories live under runner-temporary paths, tracked Engine source cleanliness is certified after test phases, and Production V4 fails closed on source mutation. Reset/restore/allowlist cleanup is not accepted as a substitute for isolation.

## Closure 3 — Runtime phase / ambient CI authority

**State:** ARCHITECTURALLY CLOSED by PR #366; current validity is re-certified by P0.

Run130 showed that workflow identity is not the same thing as live production phase. `scripts/runtime_phase.py` is the application-owned authority and requires explicit `ISCO_CANONICAL_RUNTIME=1` activation in addition to exact Production V4 identity. The historical planning-checkpoint implementation is retained only as compatibility/storage logic behind a wrapper that injects this authority before exporting any function.

PR #366 was merged only after Full Engine + Full Runner + M7 + M11 were Green on exact head `984983afa25f6898bcc7279fbf5e6c17006381cf`.

## Closure 4 — Durable planning composition / provider cache authority

**State:** ARCHITECTURALLY CLOSED after Run132; current validity is re-certified by P0/P1.

Run132 proved that individually correct checkpoint, cache and terminal-recovery features can still conflict after composition. The durable router document remains schema v1 while namespace evolution is versioned independently; a Groq prompt family becomes cache-warm only after provider-reported `cached_tokens > 0`; and terminal recovery accepts reset evidence up to 60 seconds, sleeps no more than 60 seconds per recovery, allows at most three recoveries, and caps total terminal wait at 180 seconds. These composed contracts are now explicit Stage Ladder evidence rather than historical PR claims.

## Closure 5 — Run51–209 cumulative regression / stale-stage evidence

**State:** BLOCKING until the exact current `main` SHA receives a Green P0–P6 Stage Ladder certificate.

Run50 is the last independently verified historical End-to-End success. Its `final.mp4` identity is locked by exact size and SHA256 only as an immutable QC/Gold/delivery handoff fixture. It predates the current montage stack and is not a current visual-quality baseline. A historical fix, a test named after a run, or an old success at Stage 6 is not current closure evidence.

The mandatory ladder is:
- P0 — runtime, environment, state, checkpoint, capability ownership and test/production isolation.
- P1 — planning, schema/repair, representation authority, provider routing, capacity, budget and retry ownership.
- P2 — TTS, voice, pre-TTS feasibility, audio semantics and mastering.
- P3 — media/visual feasibility, Vision health/scope, QR/security and M7–M11 cinematic bindings.
- P4 — current format-aware Final Master QC over the real immutable `video-50/final.mp4` baseline.
- P5 — current Gold same-render state transition over that staging media with deterministic recorded external boundaries.
- P6 — current packaging, unified delivery and release-transaction dry replay with zero publication.

Every Run from 51 through 209 belongs to exactly one audit cohort in `scripts/production_family_closure.json`; known repeated failure families are mapped separately to executable contracts and required phases. The exact incident ledger now covers Run184–209, including the latest twenty-run window (190–209): each Run records Short/Long format, the real failure phase, a stable failure signature, and one or more reciprocal executable families. Runs 193–195 and 197 are Planning/P1, Runs 196 and 198 are Voice/P2, Runs 199–201 are Visual/P3, Run202 is entrypoint/P0, Run203 is Long ingress/P0, and Runs 204–209 are Long Planning/P1. Within that final series, Runs 204–207 and 209 belong to split-outline transport/domain/output-headroom closure, while Run208 belongs to temporal Groq TPM-window batch recovery.

The certificate fails closed if a cohort or incident is missing/duplicated, if an incident points to the wrong phase, if its family does not point back to the same Run, if no declared family contract ran in the actual failure phase, if a family references a run outside the certified window, or if a declared family contract is absent from the tests executed on that exact SHA.

## Forward anti-staleness guard

Starting at Run171, every tracked `scripts/test_runNNN_*.py` is treated as a production incident regression contract. Stage Ladder refuses certification when either:
- the highest named regression Run is newer than `historical_window.last_run`, or
- a named Run regression inside the window is not declared by at least one family in the machine-readable register.

Starting at Run184, the stronger incident-ledger guard additionally requires exact numeric coverage through `historical_window.last_run`, phase/cohort alignment, reciprocal Run↔family membership, and at least one family contract executed in the incident's failure phase. This makes the register self-policing: a future incident cannot be hidden behind an unrelated visual family or omitted while leaving a misleading Green family-closure certificate.

## Permanent production rule

A Green ladder run on a PR is review evidence only. After merge, the ladder must run again on the resulting `main` SHA. Only a successful **push-to-main** ladder may publish `stage-ladder-green-<sha>`, and Production V4's existing environment preflight must resolve that exact ref to `GITHUB_SHA` before provider secrets are materialized.

Therefore a commit after certification invalidates production readiness automatically. The Ladder never dispatches Production V4 and never publishes a release. Production remains manual.
