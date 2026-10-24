# Approved «على مهل» podcast visual identity — installation gate

This folder receives **only the creator-approved** real circular-logo stills, **not** an AI recreation or the old square white background.

The approved visual pack has already been prepared as a downloadable ZIP in the conversation, but binary files are **not** in this GitHub branch yet. Extract exactly these files from the pack's `assets/` folder to this directory:

- `intro_16x9.png` (1920×1080)
- `prayer_16x9.png` (1920×1080)
- `outro_16x9.png` (1920×1080)

`hook_example_16x9.png` is a sample only; it MUST NOT become the fixed opening hook or replace the first 15–25-word topic-specific **A** question. `ala_mahl_original_logo_circle.png` has the owner's original emblem with transparent corners; use it for future approved branding, not as a second channel logo.

The importer still requires actual independently Claude-approved `production.json`; this visual pack is **not** an approved episode ZIP. These three files are private Runner media assets and must not be silently injected into legacy «خارج النص» or Shorts.

Runtime: for `format=podcast, template=ala_mahl_dialogue` only, `clean_v2.identity_sequence` validates PNG and 1920×1080; a real owner image overrides the old prototype, and its SHA-256 fingerprint rotates the intro/outro video cache. A missing asset retains the old prototype pending installation and MUST NOT be called final visual approval. The original prayer spoken text/audio and its position are unchanged; only the on-screen still is different. Do not import a new prayer voice.

Before merging or releasing, verify real FFmpeg intro/outro, exact circular crop (no square), no spurious typography, no cut last word, and full-screen prayer. If approved files are missing, keep release status **NOT READY**. No auto publishing or unauthorized TTS.
