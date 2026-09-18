# Clean V2 Preservation Map

Date: 2026-09-18

This document freezes the useful contracts and lessons from the legacy system. It does
not delete, rewrite, or retire legacy code. Clean V2 is an isolated production path.

## Frozen source identities

| Source | Frozen identity | Evidence |
|---|---|---|
| Runner legacy baseline | `c4b1a1bea605d7e998685a0f399aad8b89bbb2d2` | Live `main` when Clean V2 began; merge of PR #664 |
| Engine pin and Engine `main` | `3cbd689819e6b0e0b2ea9904d1998e24a5e2a293` | `.github/workflows/produce-resilient-v4.yml`; live Engine `main` |
| Approved Brief SHA-256 | `bcf8d3017ee8e18ee4808614c7c07731b0452a5ba6182e1183404f663078e129` | `STATIC_APPROVED_BRIEF_SHA256` in the frozen production workflow |
| Approved Brief source | Engine `production/approved_brief.json` at the exact Engine pin | Human-approved source of topic, format, research scope, and hard constraints |

The legacy Runner and Engine remain available as evidence. Nothing in Clean V2 may
silently alter these frozen identities.

## What is preserved in Clean V2

| Legacy lesson | Canonical legacy evidence | Clean V2 preservation |
|---|---|---|
| Explicit stage contracts | `scripts/planning_stage_contract.py`, `scripts/native_short_stage_contract.py`, `scripts/production_stage_ladder.py` | One fixed seven-stage sequence in `clean_v2.pipeline.STAGES`; every stage is journaled and cannot run out of order. |
| Bounded provider routing | `scripts/task_level_planner_router.py`, `scripts/provider_retry_ownership.py`, `scripts/planning_provider_visible_semantics.py` | One ordered pass: Gemini → Groq → OpenRouter; one call per provider; no nested or same-provider retries. |
| No-wire accounting | `provider_failure.py`, `scripts/provider_wire_attempt_contract.py`, PR #664 | Missing keys, oversized prompts, and locally blocked models record `wire_attempted=false`, `provider_attempt=null`, and consume no wire attempt. |
| Approved-Brief authority | Engine `src/isco_video_agent/brief_approval_binding.py`; Runner approved-brief ingress tests | The Engine brief is read from the exact pin. Canonical NFC JSON hashing excludes only `approved_hash`/`brief_sha256`; any mutation after approval fails before provider work. |
| Exact Engine pin | `.github/workflows/produce-resilient-v4.yml`, `scripts/preproduction_contract.py`, `scripts/workflow_hygiene.py` | V2 checks out and records the exact 40-character Engine SHA. The Engine is a pinned Brief/voice-manifest source, not the V2 orchestrator. |
| Provider-safe errors | Engine provider adapters and Runner `scripts/provider_failure.py` | V2 records bounded reason codes only; prompts, response bodies, and secrets are not written to telemetry. |
| Stock provenance | Engine Pexels/Pixabay adapters and rights manifests | Every acquired asset records provider, asset id, source URL, creator metadata, query, and local filename. |
| Local voice fallback | Frozen Piper voice manifest in Engine `security/piper_voice_hashes.json` | V2 uses Piper directly and verifies the model/config size and SHA-256 before synthesis. |

## Regression evidence worth keeping

These are the smallest high-value legacy suites. They remain documentation and regression
evidence for the old system; Clean V2 does not execute the full legacy ladder in production.

### Runner

- `scripts.test_planning_stage_contract`
- `scripts.test_task_level_planner_router`
- `scripts.test_provider_retry_ownership`
- `scripts.test_provider_wire_attempt_contract`
- `scripts.test_budget_wire_attempts`
- `scripts.test_run129_hermetic_approved_brief`
- `scripts.test_run179_engine_pin_single_source`
- `scripts.test_preproduction_contract`
- `scripts.test_production_stage_ladder_contract`
- `.github/workflows/verify-private-engine.yml` — canonical full Engine + Runner regression
- `.github/workflows/verify-production-stage-ladder.yml` — legacy P0–P6 family closure

### Engine at the frozen pin

- `tests.test_brief_approval_binding`
- `tests.test_approved_brief_orchestration_contract`
- `tests.test_no_wire_provider_attempt_contract`
- `tests.test_ai_budget`
- `tests.test_budget_wire_attempts`
- `tests.test_v4_production_authority_contract`
- `tests.test_orchestrator`

### Clean V2

- `scripts.test_clean_v2.ApprovedBriefContractTests`
- `scripts.test_clean_v2.ProviderAccountingTests`
- `scripts.test_clean_v2.CleanV2EndToEndTests`

The V2 End-to-End test performs real local audio generation, visual generation, FFmpeg
rendering, and ffprobe inspection. Provider and stock calls are fixtures so CI spends no
quota and needs no secrets.

## Preserved as evidence, deliberately not carried into bootstrap V2

The following remain untouched in legacy. They are not allowed on the V2 critical path
until one successful complete video plus four consecutive confirmation runs exist:

- Text Audit and script repair families
- Gold, Viewer Quality, Final Master, and Final Critic quality layers
- M7–M11 cinematic gates and semantic transition/card/archive layers
- adaptive planning shards, append repair, resumptions, durable caches, and learned failure memory
- Telegram control plane, release/publish approval, YouTube analytics, thumbnails, and sibling Shorts
- legacy P0–P6 certification as a prerequisite for a V2 video run

This is not a claim that those systems have no value. It is a sequencing decision: first
prove that the seven basic production stages can finish repeatedly.

## Change rule

Before stability is proven, a Clean V2 change is admissible only when it directly fixes a
failure in one of the seven stages or improves observability needed to identify that
failure. It may not add a score, critic, repair loop, secondary deliverable, release gate,
or autonomous publication step.
