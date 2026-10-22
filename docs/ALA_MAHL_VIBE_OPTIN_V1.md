# «على مهل — إحساس» V1 | Opt-in only

## What stays unchanged
- «على مهل — قصة» is **still** `format:"short", template:"ala_mahl"`, with two to four authored 9:16 story stills (legacy 5–6 allowed), up to two ambient stock inserts, and `editing:{"music":"none"}`.
- The voice, prayer, fixed intro/outro, captions, 18 MiB ZIP ceiling, Claude review, Telegram import and manual YouTube publication are unchanged. Nidaa and «خارج النص» are not affected.

## Opt-in ZIP
Only for an approved short with `template:"ala_mahl"`, add:
```json
"ala_mahl_mode": "vibe",
"editing": {
  "music": "youtube_audio_library",
  "mood": "nostalgic",
  "asset": "assets/vibe_music.mp3",
  "track_title": "EXACT TITLE FROM STUDIO",
  "artist": "EXACT ARTIST FROM STUDIO",
  "license_type": "youtube_audio_library_standard"
}
```

`mood`: `hopeful`, `melancholy`, `nostalgic`. Mood selects a restrained color profile, not a track. The author and owner choose the actual music; no AI autonomously selects it.

**Music source instructions:** Sign in to [YouTube Studio → Audio Library](https://www.youtube.com/audiolibrary). Filter to tracks with **Attribution not required** under the YouTube Audio Library standard license, select a fitting *instrumental*, download the track from within Studio, and check actual title, artist and license terms there. Put the actual downloaded file at `assets/vibe_music.mp3` in the ZIP. The client does **not** browse YouTube, rip music from a YouTube video, or query FreePD or Coverr for this mode. **Do not choose music just because its YouTube upload says “No Copyright”.**

A displayed license field is only the author's statement. The client validates file path, MP3 header, size (≤8 MiB), decoding, audio stream and minimum duration, but **cannot independently prove** the file really came from YouTube Audio Library or guarantees freedom from every future copyright claim. Keep license/source evidence and listen to the final mix before manual publication. YouTube's guidance: https://support.google.com/youtube/answer/3376882

## Real moving imagery
Author **4–7 planned scenes** across s1/s2/s3. Each scene has:
- An approved 9:16 image `asset`, a unique literal `start_text` anchor, and `hold/cut` or allowed legacy movement/transition.
- A **different** verified `stock.url` pointing to a real Pexels or Pixabay **video** page, not a photo, Coverr, generic stock search or invented URL.
- A clear visual verb, age/clothing/place continuity where humans are present, suitable colors, and rights/semantic checks.

A remote provider can still fail to download or serve a fitting clip. The approved image is the safety fallback; that scene is **reported as degraded** in `vibe-stock.json`, not silently called complete. The producer should be able to reject and swap individual clips without touching the approved narration. No Board.

## Sound and output review
The existing FFmpeg music mixer inserts the author's file only under the measured **topic** window, leaving prayer, hook and outro free of newly added music. The original voice owns timing; neither text nor vocals are re-synthesized to fit a music beat. Existing conservative mix gain/limiter applies. A mixing error fails this optional Vibe run safely rather than pretending that missing soundtrack meets the plan.

Mood profiles adjust only gentle saturation/contrast/color temperature, not a heavy Instagram-style blanket filter.

After export, review `vibe-stock.json`, `short-audio-polish.json`, `topic-audio-polish.json`, and `delivery-readiness.json`; then watch and listen to the **actual full 9:16 video** on a phone for semantic accuracy, music/voice loudness, captions, transitions, last spoken syllable and outro. The delivery is an editorial candidate even after technical success; no guaranteed human feel or retention.

## ZIP example scene
```json
{
  "id": "v1",
  "section_id": "s1",
  "asset": "assets/vibe_01.jpg",
  "start_text": "النص المنطوق المعتمد حرفيًا",
  "motion": "hold",
  "transition": "cut",
  "stock": {"url": "<real verified Pexels or Pixabay video page URL>"}
}
```

Replace this placeholder with an actual verified **video page**, and supply distinct matching fallback images. `production.json` remains schema version 1. No new engine, extra paid service, automated publishing, or broad support for arbitrary MP3 files.
