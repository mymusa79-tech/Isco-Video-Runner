# Run #109 Structural Editorial Contract — 2026-09-07

## Root cause

The Engine already exposed deterministic `editorial_room.structural_ai_flags`, but those flags were advisory. A plan could therefore carry formulaic structural patterns such as repeated `ليس ... بل ...` constructions or duplicate sentences without automatically invoking the existing consolidated Script Doctor.

## Current-owner closure

The Runner does **not** add a new provider or retry loop. Instead:

1. the canonical planning seam installs `structural_editorial_contract` immediately after `planner_quality_guard`;
2. authoritative Engine structural flags are appended to the deterministic issue list already used to decide whether the existing consolidated Script Doctor runs;
3. the same Engine detector is re-run on the final returned plan;
4. any remaining structural flag fails closed.

## Invariants

- existing Script Doctor remains the sole repair provider owner;
- no per-section provider fan-out is introduced;
- no retry count, provider order, schema, quality threshold, cultural/religious gate, duration contract, or AI budget is relaxed;
- structural detector regexes/thresholds remain Engine-owned;
- host-managed identity continues to be stripped/reapplied by the existing Script Doctor path;
- every later `build_plan` invocation, including higher-level rebuilds, passes through the same contract.

## Supersedes

This replaces the architecture proposed by Runner PR #295, which introduced a separate local structural provider loop before the explicit Planning Stage Contract existed.
