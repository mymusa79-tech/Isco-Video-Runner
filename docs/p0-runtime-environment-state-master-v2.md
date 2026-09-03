# P0 — Runtime Environment + State Master Contract V2

Status: implementation contract for canonical Production V4.

## Problem closed

P0 previously had strong individual controls, but the live-runtime phase was promoted during encrypted-memory restore. That happened before Environment Preflight, Provider Readiness, and Planning Envelope certification. The individual guards were valid, but their lifecycle authority was split: pre-production preparation could accidentally declare the rest of the workflow to be live production.

Telegram sibling Shorts also inherited the parent process environment. A sibling has a different approved brief and planning binding, so inheriting the parent's approved-input/checkpoint identity was not a safe cross-run state boundary.

## Master rule

`ISCO_CANONICAL_RUNTIME=1` is never inferred from GitHub Actions context alone.

Canonical Production V4 now has three states:

1. **Workflow identity only** — canonical workflow is recognized, but runtime is not live.
2. **Pre-production preparation** — authenticated memory/checkpoint state and the immutable approved-brief snapshot may be prepared in process-local runtime mode. This mode never writes live-runtime authority to `GITHUB_ENV`.
3. **Canonical live runtime** — only the final Planning Envelope preflight may promote the workflow to live runtime, through `p0.runtime-environment-state.v2`, after all P0 evidence is revalidated.

## Evidence required before promotion

The P0 Master fails closed unless all of the following agree:

- exact canonical Production V4 workflow identity;
- exact Runner SHA and pinned Engine SHA;
- GitHub run id and run attempt;
- environment-preflight evidence and required media capabilities;
- provider-preflight evidence with all hard providers ready;
- planning-envelope PASS with required provider-family redundancy;
- authenticated persistent-memory restore identity;
- immutable read-only approved-brief snapshot with exact SHA256;
- durable planning checkpoint identity bound to the same approved brief, Engine SHA, and planning contract closure;
- private file-backed planning checkpoint encryption key;
- private file-backed GitHub credential required for durable planning-state persistence.

Only after these checks does the Master export `ISCO_CANONICAL_RUNTIME=1` to later workflow steps and write non-secret `p0-runtime-master-contract.json` evidence.

## Telegram sibling isolation

Sibling Short subprocesses inherit the already-authorized parent bundle's live-runtime permission, but they do **not** inherit the parent's approved-brief path or immutable snapshot identity. Each child materializes its own read-only snapshot from its exact inherited approved request.

Sibling children use checkpoint mode `isolated_sibling_child_no_cross_run`. Their in-run Planning/Quality/Safety gates remain unchanged, but they have no authority to write or resume the parent's cross-run planning checkpoint. This prevents a child-specific brief from being persisted under the parent's durable planning binding.

## Safety invariants preserved

- No Quality Gate is lowered or bypassed.
- No Production Run is dispatched by this change.
- No YouTube publication behavior changes; publication remains manual.
- Existing fail-closed persistent-memory and planning-checkpoint authentication remain authoritative.
- Existing exact-SHA Stage Ladder and full regression requirements remain mandatory before merge.
- Child checkpoint isolation removes only cross-run persistence authority; it does not relax live generation, review, final QC, Gold, packaging, or delivery gates.

## Activation boundary

The intended canonical order is:

`Restore authenticated state -> Environment Preflight -> Provider Readiness -> Planning Envelope -> P0 Master promotion -> Produce`

A failure anywhere before P0 Master promotion leaves live runtime disabled.