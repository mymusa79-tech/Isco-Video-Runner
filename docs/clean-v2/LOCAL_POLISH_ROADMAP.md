# Local Production Polish Roadmap — Zero-provider policy

Status: design/audit only. Cover Studio V2 design grammar is the only newly implemented prototype in this branch.

## Policy

Every polish layer must satisfy all of the following:
- no new paid provider or API call;
- deterministic/local where practical;
- one small dependency at most when the quality gain is material;
- fail-soft for cosmetic layers;
- no new production acceptance gate unless the layer protects the final file technically;
- no retry storm;
- measurable CPU/RAM/time budget;
- reuse existing timeline, script, visual-story, and approved media rather than inventing parallel state.

## 1. Cover Studio V2 — IMPLEMENTED AS PROTOTYPE

The design-grammar prototype now supports six local families:

1. `split_cinematic`
2. `centered_editorial`
3. `bold_impact`
4. `minimal_warm`
5. `stacked_story`
6. `podcast_signature`

It also supports three typography treatments:
- `clean`
- `depth`
- `impact`

Selection is deterministic and local. It uses only:
- text word count,
- image luminance,
- simple left/right visual-detail measurements,
- a stable hash only as a tie-break between already-eligible layouts.

No semantic model, Vision model, stock search, or image-generation provider is used.

Reference-set images approved by the user are stored in:
`/Isco Video/Benchmarks/Cover Studio Reference Set/`

The canonical single benchmark remains:
`/Isco Video/Benchmarks/cover-benchmark-v2.png`

## 2. Caption Studio Lite — HIGH VALUE, BUT DO NOT DUPLICATE PR #914

Current main already has:
- FFmpeg/libass rendering;
- white/gold Arabic captions;
- phrase timing owned by the measured voice timeline;
- local shallow depth/shadow;
- role-specific Hook/Beat/Payoff sizes;
- zero provider calls.

PR #914 is already changing the same caption surface for:
- more natural Arabic RTL phrase rendering;
- lighter shadow/better breathing;
- sparse Film text;
- CTA semantic separation.

Therefore this branch must not edit `short_timed_text.py`, `visual_cta.py`, or the Film text path while #914 is open.

After #914 is resolved, the next caption improvement should remain inside libass/FFmpeg, not Pillow-per-video-frame.

Recommended Caption Studio Lite VNext:
- preserve full Arabic phrase direction instead of English-like progressive left-to-right emphasis;
- measured auto-fit width, maximum two display lines;
- separate styles by role: Hook / Beat / Payoff;
- Noto Sans Arabic Bold/Medium body with optional Noto Kufi heavy focus only for very short Hook/Payoff phrases;
- one emphasized word or one emphasized line, not continuous flashy recoloring;
- subtle depth only when the phrase is short enough;
- deterministic safe-zone placement;
- no black box;
- no extra provider;
- no local ASR model.

Why not local Whisper/forced alignment now:
- large model/download/CPU cost;
- new failure domain;
- current voice-owned section/chunk timing is already deterministic;
- the likely quality gain is lower than typography/layout improvements.

## 3. Music Studio Lite — VERY GOOD NEXT LOCAL UPGRADE

Current main already has:
- a verified CC0 FreePD music catalog;
- local cache;
- integrity hash verification;
- deterministic topic keyword selection;
- topic-only music window from Timeline First;
- mean-level normalization around -22 dB relative to narration;
- fade-in/fade-out;
- fail-safe behavior;
- no generated-noise music.

The main limitation is not the mixer. It is the tiny musical vocabulary.

Recommended upgrade:
- expand the curated verified local catalog from 3 tracks to roughly 6–9 carefully selected tracks;
- keep files cached locally with pinned identity hashes;
- add simple metadata per track: `focus`, `hopeful`, `rise`, `warm`, `calm`, `victory`, `podcast`;
- select from existing plan/visual-story tone locally; no new model call;
- never change music every few seconds;
- one bed per Short, one or very few beds per long-form piece;
- retain narration as the priority.

Optional only after listening A/B:
- very gentle FFmpeg side-chain ducking under speech.
Do not enable aggressive compression by default; pumping would reduce quality.

## 4. SFX Lite — POSSIBLE, LOWER PRIORITY

Current main intentionally disables generated SFX. That is a good default.

If added later:
- keep a tiny verified local CC0 pack (roughly 3–5 assets);
- use only for visual CTA, one section transition, or a deliberate payoff accent;
- never put a sound on every caption/word;
- deterministic placement from existing timeline events;
- local only, fail-soft, no provider.

## 5. Local Visual Grade Profiles — HIGH VALUE / LOW COMPLEXITY

FFmpeg can provide a small fixed family of channel grades without a new dependency:
- `warm_rise`
- `clean_focus`
- `soft_cinematic`

Selection can use the existing `visual_world` tone field.

Rules:
- preserve skin/object realism;
- no gloomy global grade by default;
- subtle contrast/saturation/temperature changes only;
- one coherent grade per piece, not different grades per stock clip;
- no LUT marketplace dependency.

This is likely one of the best ways to make mixed stock sources feel like one film.

## 6. Voice and mastering — KEEP SIMPLE

Current mastering is intentionally loudness-only for Charon and Nabra.

Do not add:
- generic EQ,
- de-esser,
- compressor coloration,
- pitch shift,
- tempo correction.

Those previously risked covering the approved voice character.

Safe local improvements should be limited to diagnostics/measurement unless a real listening defect is demonstrated.

## 7. Motion/Transitions — ONLY LIGHTWEIGHT

Possible local improvements:
- slightly better deterministic cut timing around existing semantic beats;
- short crossfades only where the idea transition benefits;
- no automatic Ken Burns everywhere;
- no transition pack.

The existing visual story should remain the director. Motion must not become a separate system.

## Priority order

1. Finish/validate Cover Studio V2 design grammar.
2. Resolve PR #914 and evaluate the caption result before any new caption patch.
3. Expand Music Studio Lite's curated local library and tone metadata.
4. Add one coherent local grade profile layer.
5. Only then consider a very small SFX pack.

This order maximizes visible/audible quality per unit of complexity.
