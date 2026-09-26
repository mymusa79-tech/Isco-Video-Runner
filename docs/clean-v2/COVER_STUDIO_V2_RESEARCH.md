# Cover Studio V2 — Research & Implementation Plan

Status: research/planning only. No production renderer change in this branch.

## Canonical visual benchmark

The approved visual target is the user-approved vertical cover generated on 2026-09-26:
- Library path: `/Isco Video/Benchmarks/cover-benchmark-v2.png`
- SHA-256: `923e9c4545552e7ecdc55fd0b52920819ef1880d30008dc8647a1e4e0b0130e6`
- Source dimensions: `941x1672` (near-exact 9:16)
- Benchmark phrase: `البداية الصامتة`

The benchmark is a style target, not a requirement to synthesize new scene objects. Production must reproduce its typography, hierarchy, depth, spacing, contrast, and thumbnail readability using existing approved visual assets.

### Benchmark traits to reproduce

1. Large Arabic display typography, not caption typography.
2. Two-line maximum composition with intentional line hierarchy.
3. Top line white / emphasis line gold when the phrase supports it.
4. Heavy Kufi-style Arabic weight with clean glyph joins.
5. Crisp dark outline, visible shallow 3D extrusion, then a softer separated shadow.
6. Text occupies roughly 60–78% of the canvas width and remains readable at Shorts-list size.
7. Strong breathing room around the headline; no word collision or layer collision.
8. Warm cinematic image treatment with one obvious visual subject and controlled background detail.
9. No black caption box, no CTA, no logo, no subtitle-like placement.
10. Cover text is 2–5 Arabic words and must be truthful to the actual episode/derived segment.

## Research conclusion

### Preferred local renderer: Pillow + Raqm, with FFmpeg retained for frame extraction

Pillow provides direct RTL text layout, OpenType shaping, language-aware rendering, text measurement, strokes, anchors, and raster compositing. Its Raqm path uses HarfBuzz/FriBiDi for complex-script shaping. This is a better fit for a deliberately designed Arabic thumbnail than libass, because we need per-line masks, measured auto-fit, gradient-filled text, multiple extrusion passes, and controlled shadows rather than timed subtitle rendering.

The standard Pillow wheels include a vendored Raqm path and load FriBiDi at runtime. The implementation must explicitly check `PIL.features.check_feature("raqm")` before rendering Arabic. If Raqm is unavailable, Cover Studio must fall back to the existing Cover Lite/libass renderer and must never fail the video.

FFmpeg remains the existing tool for extracting a frame from approved motion assets and for final format conversion. No new video stack is needed.

### Font decision: reuse installed Noto Kufi Arabic

The production workflow already installs `fonts-noto-core`. Ubuntu's Noto package includes `NotoKufiArabic-Bold.ttf`, so the first implementation should reuse that installed font instead of downloading a separate font asset.

This avoids:
- another network dependency,
- a font bundle in the repository,
- licensing/file-management overhead,
- another install/cache path.

A later visual A/B may compare Noto Kufi Arabic ExtraBold/Black or Cairo, but only if the built-in Bold weight cannot reach the benchmark. The renderer must never require a remote font service.

### Rejected options

**Local diffusion / Stable Diffusion / FLUX weights**
- Rejected for this layer.
- It would add large model downloads, CPU/GPU pressure, cold-start time, and a new failure domain.
- It also violates the goal of keeping the cover a lightweight deterministic sidecar.

**OpenCV**
- Not needed for V2.
- Pillow can provide the image-statistics needed for lightweight local coverability scoring without adding another large package.

**rembg / local segmentation model**
- Rejected for V2.
- It adds ONNX/model weight and is not necessary to reproduce the benchmark's typography and hierarchy.

**ImageMagick/Pango as primary renderer**
- Technically capable of complex Arabic through Pango, but adds another CLI/delegate stack.
- Pillow gives finer programmatic control with fewer moving pieces and can be feature-gated locally.
- Keep ImageMagick out unless Pillow fails an actual benchmark test.

## Proposed architecture

Keep the existing one-call Cover Lite integration point. Do not add a new pipeline stage.

`existing approved visuals -> local coverability choice -> local Pillow compositor -> cover.jpg`

No provider call. No retry loop. No cloud QA. No new stock search. No new generative model.

### Source selection

Use only assets already approved in `rights-manifest.json`.

Deterministic ranking:
- matching derived-short `section_id` first,
- hook role bonus,
- AI-still/source-quality bonus where already available,
- non-auxiliary bonus,
- then one lightweight local coverability score from an extracted still.

Coverability score is local only and should inspect at most three already-approved candidates. It may use:
- blur/sharpness proxy,
- highlight/shadow clipping,
- local contrast,
- visual density in the planned text zone.

No semantic model and no extra network call.

### Composition rules

For vertical Short covers:
- output: `1080x1920`,
- headline zone: upper-middle, approximately 20–44% canvas height,
- safe left/right margin: minimum 8%,
- two lines maximum,
- auto-fit by measured rendered width, not by fixed font size,
- target headline width: 60–78% of canvas,
- strong display scale; never reuse caption size.

For Film/Podcast:
- output: `1280x720`,
- same visual language with a landscape-specific layout profile,
- one shared renderer, not separate implementations.

### Text styling target

Default Arabic face:
- Noto Kufi Arabic Bold from the existing system font package.
- Explicit `direction="rtl"` and `language="ar"`.

Layer stack:
1. soft ambient shadow, offset and blurred;
2. dark-brown/near-black extrusion repeated across several small offsets;
3. crisp dark outline;
4. face fill.

Face fill:
- primary line: white to very-light-gray subtle vertical gradient,
- emphasis line: warm gold gradient centered around existing brand gold `#D7A85B`,
- no per-character random coloring.

The renderer should support either:
- full second-line emphasis, matching the benchmark, or
- one emphasized word when the phrase is one line.

The line break is selected by measured visual width. It must never allow glyph overlap, stacked words, or subtitle-style crowding.

### Background treatment

Use the existing source visual. Do not hallucinate new objects.

Local treatment only:
- cover-aware crop/reframe,
- modest warm grade,
- mild local contrast,
- optional subtle vignette,
- optional low-strength blur/darken behind the headline zone when needed.

Do not darken the entire image aggressively. The benchmark is warm and bright, not gloomy.

## Dependency and cost contract

Preferred new dependency: one pinned Pillow wheel only.

Before enabling:
- verify `features.check_feature("raqm") == True` in CI,
- verify Noto Kufi Arabic Bold resolves locally,
- cache pip downloads through `actions/setup-python cache: pip` where the relevant production workflow already uses setup-python.

Important: GitHub-hosted runners are ephemeral. "Install once" cannot mean one permanent machine installation unless using a self-hosted image. The practical equivalent is a pinned dependency plus GitHub's dependency cache, so repeat runs restore the wheel instead of repeatedly downloading it.

Monetary provider cost remains zero:
- no Gemini/Groq/OpenRouter/Mistral/Cloudflare call for the cover,
- no paid image API,
- no external font API.

## Performance guard

The implementation must be rejected or simplified if the local cover work exceeds the agreed lightweight budget.

Initial engineering target to measure in CI:
- <= 10 seconds incremental wall-clock for one cover on GitHub Ubuntu CPU,
- <= 250 MB incremental peak memory,
- no more than three local candidate frame extractions,
- one final JPEG write.

These are acceptance targets, not measured claims yet.

## Failure behavior

Cover Studio V2 remains fail-soft:
- `final.mp4` must never fail because of cover rendering.
- If Pillow/Raqm/font resolution/compositing fails, immediately use existing Cover Lite/libass.
- If both cover paths fail, record `cover status=skipped_failed` and continue delivery.

No retry storm.

## Validation plan

### Phase 1 — local prototype against existing production artifacts
Use archived successful Shorts as input and render Cover Studio V2 without rerunning Planning, Voice, Visual QA, or full production.

Compare directly to the canonical benchmark for:
- headline scale,
- Arabic shaping,
- line spacing,
- 3D depth,
- shadow separation,
- gold/white hierarchy,
- thumbnail readability.

### Phase 2 — deterministic tests
Add tests for:
- Raqm availability gate and fallback,
- Arabic RTL shaping path,
- auto-fit and max-two-line invariant,
- no text overlap,
- safe margins,
- derived-short section-specific copy,
- zero provider calls,
- fail-soft behavior.

### Phase 3 — one production proof
Only after the local prototype visually passes:
- integrate into the existing Cover Lite call site,
- run one Short production,
- inspect delivered cover visually,
- measure wall-clock and memory delta.

Do not create a production cohort merely to test typography.

## Non-goals

Do not:
- create a new AI cover agent,
- add a Vision QA model,
- add a thumbnail retry loop,
- download a local diffusion model,
- add OpenCV/rembg just for this,
- change the successful video pipeline's acceptance gates,
- allow cover failure to block delivery.

## Decision

Proceed with a **Cover Studio V2 local compositor** based on:
- existing approved visual assets,
- FFmpeg already present,
- Pillow + Raqm for Arabic display typography,
- Noto Kufi Arabic Bold already installed through the current Noto package,
- deterministic local composition,
- existing Cover Lite fail-soft integration point.

The benchmark is the approved quality target. The first coding step after this research phase should be an isolated local prototype renderer against old successful artifacts, not a production pipeline rewrite.


## Prototype execution — 2026-09-26

An isolated local prototype was implemented in `scripts/cover_studio_v2_prototype.py`.

It was executed against two archived successful production hook frames from the existing Clean V2 artifacts. No image-generation provider, no Planning rerun, no Voice rerun, no stock search, and no production workflow were used.

Environment observed during the prototype:
- Pillow: 12.3.0
- Raqm feature: available
- local fonts found: Noto Kufi Arabic Black / ExtraBold / Bold
- output: 1080x1920 JPEG
- measured sample render: 1.54s wall-clock
- measured maximum RSS: 162,076 KB (~158 MB)

This satisfies the initial prototype performance guard (<10s and <250 MB) on the current execution environment. These numbers are local prototype measurements only and must still be re-measured on the actual GitHub-hosted production runner before production integration.

The prototype deliberately keeps the source-selection logic out of scope. It takes one already-approved visual and applies:
- warm/local background treatment,
- Arabic RTL/Raqm shaping,
- auto-fit heavy Kufi display typography,
- two-line white/gold hierarchy,
- crisp outline,
- shallow solid extrusion,
- separated blurred shadow,
- restrained gold accents.

Two generated prototype outputs were persisted in the Library:
- `/Isco Video/Benchmarks/Cover Studio V2 Prototypes/prototype-1.jpg`
- `/Isco Video/Benchmarks/Cover Studio V2 Prototypes/prototype-2.jpg`

### Revised next step

Do not add multi-candidate coverability scoring yet.

First compare these two local-only prototype renders against the approved benchmark. If the typography/layout is accepted, production integration should replace only the current Cover Lite rendering backend while preserving:
- the same pipeline call site,
- the same `cover_text` metadata,
- the same rights-manifest source selection,
- the same fail-soft behavior.

Only investigate local multi-source selection if real production evidence later shows source choice is the remaining dominant weakness.


## Production candidate update — source diversity, podcast identity, deeper tone

After visual review of the refined covers, three additional requirements were accepted and implemented in the production candidate:

1. **Source selection is no longer hook-dominant.**
   - Cover Studio locally evaluates at most three already-approved assets.
   - It scores moderate exposure, local contrast, and a usable quiet text zone.
   - Bright lifestyle-like frames are penalized.
   - The main cover and a derived Short cover avoid reusing the same source when another approved asset exists.
   - There are still zero provider calls and zero new stock searches.

2. **Podcast identity is corrected.**
   - Program name: `خارج النص`.
   - Channel/brand: `نداء اليقظة`.
   - The episode-specific `cover_text` remains the large topic headline.
   - The footer no longer says `بودكاست نداء اليقظة`, which incorrectly made the channel name look like the program name.

3. **The default cover tone is now `deep_neutral`.**
   - Lower brightness than the earlier warm/lifestyle prototype.
   - Slightly reduced saturation.
   - Controlled contrast and a soft vignette for depth.
   - Still warm enough for the channel, but not cheerful/bright by default.

### Production wiring

The production candidate now routes the existing Cover Lite call through Cover Studio V2 when Pillow/Raqm is available.
If Pillow/Raqm/font/rendering fails, it immediately falls back to the existing libass Cover Lite renderer.
A cover failure still cannot fail `final.mp4`.

The only added runtime package is pinned `Pillow==12.3.0` in the active Clean V2 production/test workflows.
No image model, Vision provider, font service, or paid dependency is added.
