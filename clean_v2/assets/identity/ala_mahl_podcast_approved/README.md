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


## Animated intro/outro (installed 24 Oct 2026)

`intro_16x9.mp4` (6.0 s) and `outro_16x9.mp4` (6.5 s) are the owner-approved **animated** identity for `format=podcast, template=ala_mahl_dialogue` only: H.264 + AAC, 1920×1080 (1280×720 also accepted), 30 fps, with their own signature sound (soft tones; the outro adds the subscribe click). They are built on the original circular logo with El Messiri typography (SIL OFL) and a calm warm palette; the outro CTA is visual only and is never spoken.

Priority per kind (intro / outro): approved `*_16x9.mp4` → approved `*_16x9.png` (rendered as a static card) → prototype card. A present but malformed video (wrong duration ±0.15 s, unsupported size, no audio stream, unreadable or >20 MB) raises `ala_mahl_approved_identity_video_invalid:<kind>`; it never falls back silently. `prayer_16x9.png` is unchanged by this: the prayer still uses the existing prayer image until an approved still is installed.

Not exercised by these files: spoken audio, final loudness balance against dialogue on a phone speaker, and release approval. Keep the release status **NOT READY** until the real render has been heard.
