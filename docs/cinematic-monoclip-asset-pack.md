# Cinematic Monoclip (Color / Black & White) — asset handoff

**Branch:** `feature/bw-monoclip-optin-v1` (opt-in only; `main` is not affected before an authorized merge).

## What the editor must supply

An approved `production.json` plus every referenced PNG/JPEG/WebP image, relative to the
same project root. The importer **never accepts a video file** inside the ZIP. The main
stock video is referenced by its exact, editor-inspected **Pexels/Pixabay/Coverr page
URL**, then retrieved and checked by the production client.

The manifest must have:

- `format: "short"`, one of the existing Nidaa short `template` values (not `ala_mahl`).
- `visual_style: "color_cinematic"` **or** `"bw_cinematic"`, chosen by the editor
  after actually reviewing the full motion and frame safety; do not infer from text.
- Three narration sections `s1/s2/s3` and exactly one scene anchored to the beginning
  of each section. All three scenes have approved `asset` image paths to satisfy
  the backward-compatible importer contract.
- No `stock` on `s1` or `s3`. `s2.stock.url` or `s2.stock.urls` contains
  1–3 **alternative** pinned video-page links, not `mode: sequence`.
- `editing: {"music":"none"}` and
  `captions: {"font":"Cairo Bold","max_lines":2,"enabled":true}`.
- `approval: {"status":"approved","reviewer":"Claude"}` only after the
  editor actually completes that approval step. This flag is a supplied assertion,
  **not** proof that Claude reviewed it. Publishing claims must match spoken content.

The three packaged still images are import/validation inputs and emergency assets:
**the final cinematic video must play one admitted continuous real clip across the
entire measured narration**. Missing, too-short or unapproved pinned stock must
fail closed rather than quietly switching to an illustration or extending the clip.
The runtime checks real duration and pinned provenance again after voice timing.
The ZIP packer only checks the URL syntax; it cannot certify clip identity,
duration, license, face visibility, text or logos.

## Prepare import ZIP (offline, no API calls)

From the repository root, with Pillow installed:

```bash
python -m scripts.prepare_cinematic_monoclip_zip \
  --manifest /path/to/project/production.json \
  --assets-root /path/to/project \
  --output /path/to/project/monoclip-approved.zip
```

Expected project layout:

```text
project/
  production.json
  assets/
    scene_01.png
    scene_02.png
    scene_03.png
    [optional named cover image]
```

The packer copies **only referenced stills and the manifest**, uses the exact
`clean_v2.production_package.inspect_archive` validator, prints SHA-256, and
refuses to overwrite an already-created ZIP. Never ship dummy images or unreviewed
stock URLs as a finished production pack.

The normal Telegram approved-ZIP workflow is unchanged: submit ZIP, inspect summary,
confirm production manually. Import does not call TTS. Manual confirmation is still
required and does not grant permission for automatic publication.

## Identity, voice, captions and regression boundary

- Cinematic Monoclip is **one continuous clip**, without spoken prayer, logo intro,
  subscribe cue, end card, outro or background music. No artificial end padding.
- Existing Nidaa short performance routing selects **Iapetus on Gemini Flash TTS**.
  It must fail closed rather than silently switching to Flash Lite/Charon.
- Large Cairo Bold captions follow voice-owned phrase events, two lines maximum.
  The last spoken phrase must remain visible through its actual end.
- Every old Short/Film/Podcast/Ala Mahl template stays on its existing path; neither
  `visual_style` nor the Monoclip packer is a global default.
- CI's synthetic sine-wave FFmpeg check certifies media/caption flow **only**;
  it does **not** establish Iapetus sound quality or human-speech alignment.
- Before release, separately verify a truly generated Iapetus sample (only after
  explicit cost/voice authorization), the downloaded full-length pinned clip,
  captions, absence of identity cards, and full cross-format regression.
- Do **not** merge to `main` or publish content based only on the offline tests.
