# Run186: concurrent Planning layout and scene-family correction

## Evidence and scope

- Production [Run186](https://github.com/mymusa79-tech/Isco-Video-Runner/actions/runs/37830819019), job `113495376642`, ran Runner `e0376e399b5340d18e1ea3b599ddfc26417b6e61` and frozen Clean V2 Engine `595f69a28ff6ddc5532291c9f113f334562292f9`.
- The requested Short experiment used the existing micro_story template and default topic. It failed Planning after 108.711 seconds, before Script, Voice, acquisition, visual review or rendering. No final video exists for this run.
- The uploaded and downloaded `clean-v2-short-final-one-186.zip` are identical: SHA256 `49d090b0df8d7c787b51b655422e4ec03d9ea5005cbd3b40d0d43fe76d6255bb`, 5122 bytes.
- All three Mistral outputs were rejected with `visual_story section s3 exceeds 3 beats`. Each diagnostic classified stationery at b1, b3, b4, b5, b6, b7 and phone at b2. Consecutive stationery pairs were b3/b4, b4/b5, b5/b6, b6/b7. The first draft also repeated a process in the payoff.
- Gemini Flash Lite fixed enough structure to reach the family gate, but still had five stationery beats. The last existing OpenRouter fallback returned upstream HTTP429. That availability error was not the cause of the earlier invalid drafts.
- The artifact records errors, hashes, shapes and family IDs, not the complete authored rejected Planning JSON. Exact section assignments and authored beat wording cannot be reconstructed. The new regression fixture is synthetic, matching the observed overflow and six-stationery error pattern; its illustrative 1/1/5 distribution is not claimed as captured output.

## Correctable contradiction

The existing repair prompt had no targeted section-overflow correction. Its generic conflict instructions said `Preserve each beat's section_id`, even when those assignments were invalid. The same response also requested the Short's 3/2/2 cut. The provider therefore received conflicting correction directions while spending its existing bounded attempts.

Family feedback counted the overused family but did not identify the excess scenes to replace. It also insisted on preserving a scene's literal meaning_target/viewer_intent even when that unapproved Planning depiction was itself locked to the rejected prop. Changing only the English search string could not remove six notebook scenes coherently.

These are confirmed instruction defects. They do not establish that every model failure has the same cause or guarantee future production success.

## Change

1. Mistral's existing strict Planning response schema now declares the Short's three section IDs and seven ordered beat slots, with section references s1/s1/s1/s2/s2/s3/s3. The raw role contract remains first hook, middle body, last payoff; the existing house-cut normalization still owns hook coverage. This uses the same prefixItems pattern already present in Mistral Script.
2. Host diagnostics report existing section-to-beat IDs, invalid Short assignments and deterministic excess/consecutive family beat IDs. They are feedback only: no host-authored replacement story, query or semantic rewrite is accepted locally.
3. Correction instructions fix layout and family defects together even when a different error appears first. Invalid section assignments may change. Failed prop-dependent Planning visual fields may be re-authored together to prove their section purpose, while preserving episode meaning, section purposes, story arc, hook tension, payoff answer and the specific practical action.
4. Groq keeps its reusable strict item schema and nullable authored alternate. Mistral's per-index schema does not leak to Groq. Gemini keeps its existing compact schema. Long and Podcast do not inherit the seven-beat Short cut or its global two-use limit.
5. The new regression module is mandatory in Stage Ladder P1. No existing test or validator is removed or relaxed.

The patch changes no production workflow, provider route/model, attempt limit, prompt ceiling, Voice, Timeline, retry/resume implementation, Engine pin, delivery rule or quality/safety gate. It adds no paid service or extra live provider call. Per-index schema adds request tokens for Mistral; fewer wasted correction attempts is the intended benefit, not a measured cost reduction yet.

## Verification

- New regression module: 24 tests passed locally, including all 14 selected templates and production-validator/router composition.
- Focused local suite: 218 tests passed, covering existing Planning feedback, unified visual story, provider schemas, capacity headroom and Stage Ladder registration. Candidate and postmerge certifications must use the canonical pinned Engine; the local Engine environment is supporting evidence only.
- Synthetic provider responses verify exact feedback, unchanged valid content, bounded fallback and continued rejection of invalid output. They do not simulate or prove live model compliance.
- Full candidate regression, the other six required candidate workflows, both postmerge main workflows and exact-SHA certification refs are required before treating this patch as ready.
- No second production run is launched as part of this correction. Actual scene-selection improvements from PR1071 were not reached by Run186 and still need a future successful live pipeline to assess.

## Primary source basis

[Mistral custom structured outputs](https://docs.mistral.ai/studio/conversations/structured-output/custom) documents schema-constrained output with response_format. It does not explicitly establish every supported JSON Schema keyword in the page reviewed. Reusing the repository's existing Mistral Script tuple-schema pattern is an implementation inference, with local schema/wire tests and production validation retained.

This patch addresses the observed Planning loop without introducing another model, validator, repair stage or search provider. It does not claim to close all historical production failure families permanently.
