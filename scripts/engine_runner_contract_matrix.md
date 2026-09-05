# Engine ↔ Runner Planning Contract Matrix

This file documents the CI-enforced compatibility boundary for the pinned Engine.

- Provider Core DTO: 8 authored EditorialIntent fields only; host metadata is forbidden in provider output.
- Canonical EditorialIntent: provider fields + `editorial_fingerprint` + `persona_version`, recomputed by the Engine.
- Provider Sections DTO: `section_briefs` only, exact count.
- Canonical assembled outline: strict full outline + canonical EditorialIntent; host metadata must round-trip exactly.
- Canonical ProductionPlan projection: `plan.json` must equal `ProductionPlan.to_dict()` apart from the post-certification `plan_source` annotation.
- Pinned Engine model fields for EditorialIntent, ProductionPlan, and ScriptSection are checked fail-closed at runtime and in regression tests.

Any field drift requires an explicit Runner contract update before production certification can pass.
