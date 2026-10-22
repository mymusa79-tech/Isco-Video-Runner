# Approved ZIP visual editing: podcast vs narrated video

This document is an **editorial reference** for the external ChatGPT author and Claude reviewer. It does not change script/voice generation, publishing, or the already approved identity media.

## «خارج النص» / podcast: natural photo coverage, not the long-film montage

- The A/B conversation carries the meaning. Start with **the entire first A question/turn**, including all its sentences; only then the existing 6-second V8 intro, approved prayer, and B's answer. A full stop inside the first A turn is **not** an opening cut.
- Aim for **4–7 distinct photographic stills per major script section** only when the ideas justify that coverage. These are a target, not a forced number, and should represent real actions/places/objects mentioned in that section. Prefer natural observational photos with authentic lighting/colours and no conspicuous AI look; avoid repeated symbolic props, posters, faces, logos, text overlays, generic desks/sunrises, artificial glow, or monochrome filters.
- Mostly use `motion: "hold"` with `transition: "cut"` or occasional restrained `dissolve`. Deliberate limited `slow_pan` and `slow_zoom` are supported but must be rare, not an automatic effect. No rapid montage, explanatory boards, aggressive punch-ins or long-film effects.
- Still image stays unchanged until a **meaningful new idea**; use the exact `scene.start_text` anchor. Stock inserts are optional and uncommon, never obligatory. The approved still is always the fallback.
- The old one-image podcast package is backward-compatible and continues to play static, but it should **not** be reused for a new episode requiring varied coverage.
- Keep every episode cover and derived-short cover as an actual approved asset in the ZIP. Never ask the renderer to invent another one. An image at the exact target JPEG dimensions is preserved without recompression.

## «نداء اليقظة» regular Short / Film: bounded alternative stock links

- On a scene where real stock adds meaning, the editorial package can include `"stock": {"urls": ["https://...best", "https://...backup", "https://...third"], "must_show": "..." }`. Use **actual checked Pexels/Pixabay/Coverr clip or photo page URLs**, not search result links or guessed identifiers. 1–3 unique links are valid and retain the supplied priority order.
- The older single pinned `"stock": {"url": "https://..."}` remains valid. A scene may not have both `url` and `urls`; do not repeat the same clip, even between scenes.
- The client tries pinned candidates sequentially if fetching, decoding, safety/preflight, or duration checks fail. An accepted candidate ends the retries; if all fail, use the ChatGPT-approved scene still and explain the fallback in the report. More links do **not** guarantee the pixel-semantic match or a successful download. The downstream picture review still applies.
- Do not insert a stock clip just because the link works; the editor must check that what it depicts supports the spoken sentence. The visual meaning and calm pacing are more important than the number of downloads.

## Production guardrails

- The imported ZIP is `production.json` plus `assets/`. A new multi-image podcast may use up to 40 scene anchors; import limit is still **18 MiB compressed**, so export photographic JPEGs at a sensible quality. Three linked URL candidates do not download any video into the ZIP.
- Respect approved scripts, voices, prayer, intro/outro, image ownership, rights and manual-only YouTube publishing. No new provider, paid workflow or automatic merge.
- Before approval, listen to the A question **continuously from first to last word**, inspect visual rhythm from the actual rendered video, and confirm the exported cover's file identity.
