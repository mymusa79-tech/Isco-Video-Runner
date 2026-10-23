# «على مهل ☕» — opt-in podcast (draft)

**Two distinct channels.** «نداء اليقظة» keeps its ordinary Short/Film and «خارج النص» podcast unchanged. The existing «على مهل» channel keeps its story Shorts and gains an **optional separate** long-form podcast; no YouTube upload is automatic.

## ZIP routing

Author a production ZIP using the existing schema v1 and:

- `"format": "podcast"`
- `"template": "ala_mahl_podcast"` (the only new allowed combination)
- `"roles": {"A": "listener", "B": "host"}` (stable protocol keys, *not* on-screen character descriptions)
- `script.sections`: 2–5 sections of approved ordered `turns`, with each turn carrying `speaker` and `text`.
- `editing.music`: `none` or an existing owner-supplied and hash-validated approved `youtube_audio_library` podcast track.
- Existing cover, caption, voice and photographic scene validation is reused.
- Do not include prayers, spoken channel definitions, or outro text inside `script`; the identity renderer owns these.
- Only manually reviewed and actually approved ZIPs may be marked approved after Claude review.

The legacy `"template": "dialogue_qa"` still selects **خارج النص** (A=Orus, B=Charon) and its V8 cards. `"template":"ala_mahl"` remains exclusively the existing **Short** series. An invalid combination fails closed; the ZIP cannot choose raw voice names or arbitrary brand assets.

## Editorial contract — «الموقف من وجهين»

1. Open with A's concrete situation and genuine question, not greetings or a generic maxim.
2. A (Iapetus) is **a thinking participant**: proposes a plausible first judgment, raises a real objection or counterexample, and may be right. A is not a pretended naïve interviewer.
3. B (Charon) examines facts and unknowns, challenges overconfidence, offers grounded human interpretation and a practical distinction. B may concede to A.
4. Each episode commits to **one central situation** in family, friendship, work or ordinary human decisions. The turning point must add *new* evidence or a fair counterargument. Avoid overused suffering/redemption formulas, invented documented events, pseudo-therapy, moralizing, and fake disagreements.
5. End with a *conditional conclusion*: when each judgment is sensible, what is unknown, and one doable action where relevant. No forced platitudes or guaranteed universal cure.
6. Real stories require source checks. Fictional examples must be presented as illustrations, never confirmed news.
7. Word-level dialogue gate: the opening A turn must contain a question, then B immediately follows; every A turn must be followed by B, max **55 Arabic words per A turn**, and A makes up no more than **55% of total words**. These are safety ceilings, not writing targets. In ordinary practice, aim shorter.
8. Typical **8–15 minutes** only when the topic warrants it. Audio measured duration wins; never fill time with repeated advice. The main script is authored and approved outside the renderer.

## Approved voice and brand isolation

- Opt-in podcast: A=**Iapetus** (reused from Nidaa Shorts), B=**Charon**.
- Existing `خارج النص`: A=**Orus**, B=**Charon**.
- Existing ordinary Nidaa Shorts: **Iapetus**, unchanged.
- Existing «على مهل» story Shorts: unchanged.
- No swapping voices mid-episode, no TTS provider fallback, no new paid vendor. The user must audition the paired Arabic voices before publishing.
- The 16:9 Ala Mahl podcast intro/outro are **separate** and must contain no «نداء اليقظة» or «خارج النص» names. The built-in new **round wordmark is provisional** and requires owner visual approval against the already approved Ala Mahl circular Shorts logo. Do not claim it is an exact extracted logo.
- Existing prayer picture and approved text remain shared unchanged; no extra prayers.

## Montage and material

Reuse the restrained photographic podcast render path from #1208: photographic 16:9 actual people/objects/places, meaningful scene progression, optional licensed stock, no static picture repeated across the entire episode when the idea develops, no dynamic pseudo-film/collage, no faces or unwanted logos. A/B speaker switches **do not** force image switches. Each ZIP includes actual fallback images. If the selected stock fails, the approved photo remains.

The existing video style and music registry have not been independently audited by this addition; a real end-to-end episode is required before release. Do not assume a source link means its visual meaning matches the words.

## Acceptance gates before production release

1. New template must import and map to distinct voice mode. Existing `dialogue_qa` must remain byte-compatible and keep Orus/Charon.
2. Generated intro/outro both decode and have original timing, stay inside the measured audio-owned timeline, show «على مهل» branding only.
3. Genuine two-voice TTS request must succeed and be auditioned. A prior Google TTS schema fix is **not** proof a real call was accepted.
4. Complete FFmpeg render: no talking cut, prayer once only, no video/audio discontinuity, correct music mix, captions, final cover and optional derivative.
5. Negative regression suite for normal Shorts, Ala Mahl story Shorts, Film and «خارج النص».
6. Claude checks final script, source rights, ZIP scenes and image assets before APPROVED. The owner alone performs Telegram import and manual publication.
7. No main merge without the owner's separate explicit approval.

**Status:** opt-in implementation is draft until CI checks and perceptual full sample pass.
