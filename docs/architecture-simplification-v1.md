# Architecture Simplification V1

Status: audit / migration contract only. No production dispatch. No quality threshold changes.

Baseline Runner head: `cf0271363cc431f9088d8d2799c648a4645dfaa9`
Certified Engine pin: `3cbd689819e6b0e0b2ea9904d1998e24a5e2a293`

## Objective

The production objective is not to maximize gate count or recovery code. It is to maximize the probability that a valid, high-quality approved request reaches an accepted final artifact without a full restart.

Acceptance target for the next controlled benchmark:

- 15 fresh approved production requests.
- 10–12 accepted high-quality videos without relaxing existing quality thresholds.
- A bounded checkpoint resume is recovery of the same attempt, not a new production attempt.
- At most two full regenerations caused by infrastructure/provider failure.
- Every terminal failure must have one authoritative owner and one typed reason.

The architecture should optimize end-to-end reliability. Ten independent 95% hard gates yield only about 59.9% end-to-end pass probability; five yield about 77.4%. Therefore a new terminal gate is not free. A check should hard-block only when its failure proves that publishing would be unsafe, invalid, corrupt, rights-incompatible, or below an explicitly owned final quality contract.

## Findings

### 1. The repository has accumulated incident patches instead of absorbing them into canonical owners

`planning_runtime_contract.py` is the clearest example. The canonical planning path installs many independent patches and guards, including `attempt9_*`, `attempt10_*`, `run120_*`, `run123_*`, `run124_*`, `run125_*`, multiple capacity layers, multiple schema/repair layers, and repeated reassertion of the Stage Contract.

These fixes were individually justified, but they now form production behavior through installation order and monkey-patch composition. Historical incident identity should live in regression tests and commit history, not in permanent runtime module ownership.

### 2. Planning currently has a canonical owner plus a guarded legacy owner

`planning_stage_contract.py` is the intended canonical owner for stage identity, schema, semantic validation, provider policy, and checkpoint policy.

`task_level_planner_router.py` still contains an older provider loop and historical checkpoint helpers. Its own comments acknowledge that two provider-loop implementations previously competed for `resilient_planner.json_text`. `planning_legacy_authority_guard.py` then has to disable the old checkpoint reader/writer so that the legacy module cannot regain authority.

A dormant authority that must be monkey-patched to stop it owning state is architecture debt. The end state must physically remove that authority rather than permanently guard it.

### 3. Audio authority is excessively fragmented

`audio_production_contract_v2.py` documents eight different owners across synthesis retry, cache, source conditioning, normalization, compliance repair, provenance, semantic fidelity, and final media gating.

The useful invariants should remain, but one audio stage should own the lifecycle. Provider availability must not be confused with proof of bad audio. Deterministic media defects remain hard failures; an unavailable semantic auditor is a recoverable quality-check state, not evidence that the final audio is semantically wrong.

### 4. Final quality and recovery have overlapping state owners

The canonical entrypoint can capture `AUDIO_QC_PENDING` and generic `QC_PENDING` around the same post-render failure. Separate Gold and Audio resume workflows add more recovery authorities around the same final artifact.

There should be one durable `QUALITY_PENDING` record containing:

- exact final artifact SHA-256;
- failed quality check id;
- typed failure class;
- provider/capacity evidence if applicable;
- remaining bounded attempt budget;
- exact next action;
- immutable production identity.

One resume controller should resume the failed check without re-running completed deterministic work or rebuilding the video.

### 5. Workflow YAML owns application behavior that belongs in Python

`produce-resilient-v4.yml` performs ingress validation, exact-SHA certification, control-plane state decryption/mutation, approved-brief validation, dependency/bootstrap logic, Piper cache validation/download behavior, security checks, state restore, and production orchestration.

GitHub Actions should orchestrate trusted commands, not be a second application runtime. Moving these behaviors into versioned Python commands makes them testable, composable, and easier to reason about.

### 6. The Engine/Runner authority boundary itself is good and should stay

Keep the existing fundamental split:

- Engine: deterministic/editorial/media library and contracts; development/dry-run workflow only.
- Runner: sole real production authority.
- Telegram: request/admission/control interface only; never direct media production authority.

The simplification should strengthen this boundary, not merge the repositories into one large application.

### 7. The Engine already demonstrates the right simplification pattern

`resilient_planner.py` moved away from per-section provider fan-out toward a bounded whole-script flow: split editorial outline, one full-script call, and one optional consolidated Script Doctor repair. That reduced provider-call multiplication while preserving deterministic final checks.

Use that same pattern for the rest of production: one owner, bounded attempts, targeted repair, then a final authoritative decision.

## Target authority model

Production should have five hard lifecycle stages:

| Stage | Sole lifecycle owner | Hard-block examples | Recover inside stage |
| --- | --- | --- | --- |
| PLAN | planning service | invalid approved input, unresolved structural/semantic contract after bounded repair | provider failover, schema repair, targeted plan repair |
| ASSETS | media service | unsafe/unlicensed asset, no valid candidate after bounded search | alternate query/provider/candidate |
| AUDIO | audio service | corrupt/missing audio, confirmed semantic mismatch, unrecoverable A/V incompatibility | TTS failover, local fallback, mastering repair, auditor failover |
| RENDER | render service | render failure, corrupt final container, deterministic A/V failure | cache/re-render of failed deterministic step |
| ACCEPT | acceptance service | confirmed final quality block, rights/security failure, invalid final-byte provenance | auditor failover or `QUALITY_PENDING` checkpoint |

Only the pipeline state machine owns stage transitions, checkpoint creation, and resume. Provider adapters do not own stage state. Critics do not own retry policy. Cache modules do not own semantic acceptance.

## Failure taxonomy

Every failure should map to exactly one of three classes before it leaves a stage:

1. `TERMINAL_CONTENT`: deterministic or independently confirmed content/safety/rights/quality defect. The attempt fails unless an explicitly allowed targeted repair succeeds.
2. `RECOVERABLE_TECHNICAL`: 429, capacity, timeout, 5xx, malformed provider output, unavailable auditor, transient download/provider failure. Fail over or checkpoint; do not regenerate completed work.
3. `INTERNAL_BUG`: contract/authority/state inconsistency. Fail fast with diagnostics; do not disguise it as provider or content quality failure.

`AUDIT_UNAVAILABLE` belongs to `RECOVERABLE_TECHNICAL` unless the audit is legally/security-mandatory and no qualified alternate mechanism exists. It is not proof of a semantic defect.

## Concrete consolidation map

### Planning

Canonical destination modules:

- `planning/stage_contract.py`: explicit stage identity, schema and semantic validation only.
- `planning/provider_mesh.py`: provider eligibility, one normalized failure taxonomy, capacity evidence, bounded failover/retry only.
- `planning/recovery.py`: schema repair and semantic targeted repair only.
- `planning/checkpoint.py`: one versioned durable planning checkpoint owner.

Migrate and then retire production authority from:

- `task_level_planner_router.py` legacy cache/schema/routing authority;
- `planning_legacy_authority_guard.py` after the legacy authority is physically absent;
- `attempt9_schema_normalizer.py`;
- `attempt10_append_bound_recovery.py`;
- `run120_dossier_repair_hardening.py`;
- `run120_schema_policy_bridge.py`;
- `run123_budget_closure.py`;
- `run124_terminal_provider_recovery.py`;
- `run125_cache_prefix_contract.py`;
- `run125_capacity_routing_closure.py`;
- overlapping planning capacity wrappers after their behavior is covered by `provider_mesh.py`.

Run-specific history moves to regression test names/fixtures and commit history. It must not remain a production architecture boundary.

### Provider reliability

Create one shared provider-call policy used by Planning, Text Audit, Vision, Audio Audit, and Gold:

- typed classification;
- one wire attempt by default;
- retry only on an explicit retryable class and bounded wait budget;
- provider/model failover according to capability eligibility;
- no nested independent retry loops;
- one run-scoped capacity ledger;
- no quality threshold logic in the provider layer.

The existing retry-ownership invariant is valid. Replace source/AST topology assertions over time with behavioral contract tests against the shared policy.

### Audio

Consolidate the current ownership map into:

- `audio/pipeline.py`: synthesize/fallback, conditioning, mastering, bounded permitted repair;
- `audio/quality.py`: deterministic checks plus semantic-fidelity confirmation;
- `audio/cache.py`: exact-input/output durable reuse.

Do not hard-fail a production artifact because one semantic auditor is technically unavailable. If both qualified auditors are unavailable after bounded routing, persist `QUALITY_PENDING` on the exact final SHA and resume that audit later. A confirmed mismatch still blocks at the existing threshold.

### Final quality / Gold

Create one `AcceptanceReport` for the exact final SHA. It composes deterministic evidence and semantic critics but owns only one final state transition.

Gold, Viewer Quality, Final Critic, Audio QC, and Final Master may remain distinct checks internally, but they must not each own independent retry/resume/state machines.

Collapse `audio_qc_pending`, generic `qc_pending`, and Gold-specific pending state into one typed `QUALITY_PENDING` checkpoint and one resume entrypoint. Retire separate resume workflows only after exact-SHA resume regression proves parity.

### Workflows

Target active production/control surface after migration:

- `ci.yml` / certification;
- `production.yml`;
- `resume.yml`;
- `telegram.yml`;
- dedicated deploy/observer workflows only where they truly require a separate trigger.

Move one-time `_patch-*` workflows out of the active Actions directory after confirming no current state record references them. Preserve them in Git history; do not keep emergency archaeology as permanent user-facing production surface.

Move inline application logic from `produce-resilient-v4.yml` to tested Python CLIs. The workflow should checkout, certify, bootstrap, invoke the production CLI, persist artifacts/state, and notify.

### Repository hygiene

- Production modules use capability names, not incident/run numbers.
- Runner tests move from `scripts/test_*.py` to `tests/` as touched.
- Historical worker versions are removed from the deployable tree once the active entrypoint/import/deployment references are proven.
- Compatibility shims such as `provider_failure.py` stay until the script/package entrypoint discrepancy is eliminated.
- `final_master_acceptance_v2.py` + legacy implementation become one canonical implementation only after byte/provenance regression proves equivalence.

## What must not be weakened

Do not lower or bypass:

- security and secret boundaries;
- media trust and supply-chain integrity;
- rights/licensing checks;
- exact final-byte provenance;
- deterministic A/V and playable-container checks;
- approved-brief binding;
- factual/religious safety constraints;
- existing quality thresholds merely to improve pass rate.

Reliability must improve by eliminating duplicated authority, provider fragility, whole-run restarts, and false terminal classification—not by accepting worse videos.

## Migration order

### Phase A — measure before deleting

Add one production outcome summary that records stage, typed failure class, whether recovery happened, provider calls, full-regeneration count, and final accepted SHA. Establish the 15-attempt baseline.

### Phase B — planning authority collapse

Make the Stage Contract/provider mesh/checkpoint implementation the only physical planning authorities. Fold run-specific patches into those owners, preserve regressions, and remove the legacy guard only when there is no authority left to guard.

### Phase C — unified quality pending

Introduce `QUALITY_PENDING` and a single exact-SHA resume controller. Adapt existing Audio/Gold pending writers to the new record before deleting old workflows.

### Phase D — audio owner collapse

Consolidate the eight documented audio owners into pipeline/quality/cache while preserving current deterministic and semantic thresholds.

### Phase E — workflow and compatibility cleanup

Thin production YAML, archive one-time patch workflows, consolidate final-master compatibility code, move tests, and remove run-number production modules whose behavior has migrated.

## Stop conditions

A cleanup change is not complete merely because lines were deleted. Each batch must prove:

- one named owner exists for every invariant being migrated;
- no second dormant owner can regain authority by import/install order;
- Full Engine + Full Runner regression is green on the exact candidate SHAs;
- production Stage Ladder / final exact-SHA checks remain green;
- no quality threshold was lowered;
- no new provider calls were added without removing an equal or larger source of call multiplication;
- a failed technical audit resumes from exact final bytes rather than regenerating the video.

## Decision

Do not rewrite the system from scratch and do not merge the two repositories. The correct move is an authority-collapse refactor: keep the proven contracts, eliminate competing runtime owners and incident-named patches, reduce hard lifecycle transitions to five, and make technical unavailability recoverable from durable exact-SHA checkpoints.
