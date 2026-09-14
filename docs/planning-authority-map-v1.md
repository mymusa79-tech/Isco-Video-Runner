# Planning Authority Map V1

Status: evidence-backed authority closure for the first simplification refactor.

## Final owners

| Responsibility | Canonical owner | Compatibility/helper surfaces | Authority rule |
| --- | --- | --- | --- |
| Stage identity | `scripts/planning_stage_contract.py` | `scripts/native_short_stage_contract.py` publishes named Moment Draft/Review/Repair operations | Prompt wording and provider-call ordinal have zero authority. |
| Output schema | `scripts/planning_stage_contract.py` via StageSpec/RequestContract and `_explicit_schema_adapter` | Provider transports in `task_level_planner_router.py` consume the explicit adapter | `task_level_planner_router.py` must not infer schema from prompt text. |
| Structural + semantic validation | `scripts/planning_stage_contract.py` plus explicit Stage extensions such as native Short | Provider helper guards may reject obviously unusable transport output | Helper validation cannot redefine the Stage acceptance contract. |
| Provider eligibility / bounded attempt policy | `scripts/planning_stage_contract.py` | `task_level_planner_router.py` supplies transport/failure/retry primitives; Mistral readiness is a helper | Stage policy owns who may be attempted and the budget. |
| Provider transport | `scripts/task_level_planner_router.py` | Gemini/Groq/Mistral/OpenRouter adapters | Transport does not own quality thresholds, Stage identity, or durable state. |
| Failure classification / telemetry | shared provider failure primitives + `task_level_planner_router.py` telemetry helpers | capacity wrappers may enrich evidence | Observation cannot become a new acceptance authority. |
| Durable Planning cache | `scripts/planning_stage_contract.py` (`_load_checkpoint_strict`, `_cache_read`, `_cache_commit`, `_save_checkpoint`) | `task_level_planner_router.CACHE_PATH` is only the shared path handle | Exactly one durable response-write authority; legacy version-1 prompt-hash cache is removed. |
| Checkpoint document namespace/composition | Stage Contract composed with `scripts/checkpoint_namespace_guard.py` | `planning_legacy_authority_guard.py` remains temporary defense-in-depth | Legacy cache helpers are fail-closed and cannot regain ownership. |
| Native Short lifecycle identity | `scripts/native_short_stage_contract.py` | Engine `active_planning_operation()` / repair context supply named operation | Draft/Review/Repair are explicit; no prompt inference. |
| Runtime install order / reassertion | `scripts/planning_runtime_contract.py` | individual installers only through this seam | Runtime composition may reassert owners but may not create a second owner. |
| Dialogue build-plan compatibility | `task_level_planner_router.install_router()` | `orchestrator.build_plan` wrapper | Retained; unrelated to schema/cache authority. |

## Legacy call-site closure

### `_structured_schema_for_prompt`

Historical authority: parsed prompt phrases such as `section_briefs`, `with EXACTLY ... entries`, and a literal section-repair phrase to choose schemas.

Current closure:
- the implementation in `task_level_planner_router.py` is a fail-closed compatibility symbol and contains no prompt selectors;
- `planning_stage_contract.install_planning_contract_router()` replaces the symbol with `_explicit_schema_adapter`;
- `_explicit_schema_adapter` ignores the prompt and resolves only the active `PlanningStageSpec` / `PlanningStageContract`;
- low-level provider helpers continue calling `_legacy_schema_hint()` only as a compatibility seam to that explicit adapter.

Result: no production schema decision can originate from prompt text.

### `_load_checkpoint` / `_save_checkpoint`

Historical authority: version-1 prompt-hash response cache owned by `task_level_planner_router.install_router()`.

Current closure:
- both legacy symbols are fail-closed and perform no filesystem I/O;
- `install_router()` no longer calls either symbol and contains no `CACHE_PATH` access;
- Stage Contract owns strict version-2 load, revalidation/eviction, and the single durable commit;
- `planning_legacy_authority_guard.py` remains temporarily as defense-in-depth during this first production-stability cycle.

Result: one durable Planning cache owner.

### `install_router`

Retained because it still owns live compatibility behavior that is not duplicated by this refactor:
- provider transport helpers and fallback plumbing used by compatibility tests/callers;
- provider telemetry and attempt bookkeeping;
- bounded retry primitives used by Stage Contract adapters;
- Mistral free-only readiness helper;
- `dialogue_qa` build-plan wrapper;
- provider-use reporting.

It no longer owns durable cache or schema selection.

### Native Short

`native_short_stage_contract.py` dynamically routes standalone Moment calls into the canonical Stage Contract under an explicit named operation scope. Therefore Native Short does not require prompt-derived schema authority or the legacy durable cache. The compatibility provider helpers remain available beneath the Stage boundary.

## First-refactor safety boundary

This change intentionally does **not**:
- remove `task_level_planner_router.py`;
- rewrite the provider mesh;
- change provider attempt budgets;
- change quality, semantic, cultural, security, Gold, Audio QC, or Final Critic thresholds;
- change Engine pinning;
- change Final Critic section ceiling 42;
- dispatch Production;
- merge to `main`.

The hypothesis under test is narrow: **removing duplicate schema/cache authority reduces state ambiguity without changing accepted quality.**
