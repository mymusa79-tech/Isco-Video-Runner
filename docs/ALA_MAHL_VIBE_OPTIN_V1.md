# Ala Mahl — Vibe (opt-in, ZIP v1 compatible)

## Default remains unchanged
Unspecified `ala_mahl` is the existing **Story** mode: 2–4 authored vertical
images (legacy 5–6 supported), up to two ambient/object stock inserts, no
added background music, original prayer/identity/outro and manual Telegram import.

## Opt into Vibe explicitly
Only a **Short** ZIP with `template: "ala_mahl"` may add:

```json
"ala_mahl_mode": "vibe",
"editing": {
  "music": "vibe_instrumental",
  "mood": "hopeful",
  "track_id": "wonder-flow"
}
```

Valid moods: `hopeful`, `melancholy`, `nostalgic`. The author selects one
verified FreePD **CC0** track in the allowlist enforced by the importer:

- hopeful: `peace-in-sunlight`, `wonder-flow`,
  `other-side-now`, `acoustic-shifter`
- melancholy: `calm-sketch-piano`, `a-little-faith`
- nostalgic: `wonder-flow`, `a-little-faith`

The track is downloaded (or retrieved from local cache) by the existing
`music_library` from the pinned, hash-verified CC0 catalog. The ZIP does
**not** carry music binaries. There is no paid provider and no song selection,
lyrics, external music search, Coverr music call or TTS regeneration.

## Visual authoring
Vibe needs **4–7 scenes**, covering each of the three spoken sections. Every
scene includes its original **portrait 9:16 image fallback** and a *different*,
editor-checked `stock.url` pointing to a **video** page on Pexels or Pixabay.
No photographs as stock, Coverr, generic text-to-stock search or Board.
All other `production.json` scene fields and literal `start_text` alignment
keep the existing contract; no new container format, audio file or video binary.

Example scene (illustrative URL only; author must verify real page):
```json
{
  "id": "v1", "section_id": "s1",
  "asset": "assets/vibe_01.jpg",
  "start_text": "أول كلمات النص المعتمد حرفيًا",
  "motion": "hold", "transition": "cut",
  "stock": { "url": "https://www.pexels.com/video/1234567/" }
}
```

**Do not reuse the illustrative URL as evidence:** resolve each real video
page, clip identity, licensing and visual match before making an actual ZIP.
Editing remains authored outside the production engine. The engine admits
clips via the existing pinned-stock acquisition and safety checks and uses
the authored image if any video cannot be used. This still produces a video;
`vibe-stock.json` reports degraded real-video coverage.

## Music, color and delivery
- The original measured human/voice track remains authoritative.
- The existing FFmpeg audio polish mixes the specifically chosen instrumental
  FreePD track under the voice in the measured **topic** window only, at the
  existing short-volume constraints with limiter. Prayer, hook and outro
  are excluded. The source is CC0 with pinned blob digest; no new music API.
- `vibe_hopeful`, `vibe_melancholy`, `vibe_nostalgic` are restrained
  natural-look grades. No dark-blue or orange blanket LUT.
- If a stock clip is missing or a licensed track cannot be retrieved, the
  original export is not overwritten by a broken file. A delivery marked
  `partial_delivery` **requires human review before manual publishing**.
- Check `vibe-stock.json`, `topic-audio-polish.json`,
  `delivery-readiness.json`, then inspect the **real final.mp4** on a phone
  for picture–voice alignment, emotional arc, captions, music loudness,
  final word and outro.
- The same strict ZIP 18 MiB, approved Claude flow and manual Telegram/YouTube
  publishing remain in place. No automatic upload, merge or publication.

## Not implied
This mode is not evidence of guaranteed emotional impact, better retention,
or automatically human-quality licensed music selection. The author makes the
editorial decisions and must review the actual produced video.
