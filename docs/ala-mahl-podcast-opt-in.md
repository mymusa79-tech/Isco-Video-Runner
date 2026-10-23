# «على مهل» — independent podcast (draft, opt-in)

## Channel isolation

«على مهل» is a **separate YouTube channel** from «نداء اليقظة».
This branch **does not change** «خارج النص» or «على مهل» story Shorts.
It extends the existing Podcast engine only for a new *manually approved* ZIP:

- `format: "podcast"`
- `template: "ala_mahl_dialogue"`
- `roles: {"A":"listener","B":"host"}` (the existing safe schema role keys)
- `script.sections`: 2–5 sections; A/B labels as distinct turns, no identity/prayer in text
- other existing production ZIP fields unchanged; no new free-form speaker/voice/provider field

Existing `template: "dialogue_qa"` is unchanged and stays **Orus/Charon**, with the established «خارج النص» V8 graphics. New `ala_mahl_dialogue` routes only its dialogue units to **A: Iapetus / B: Charon**, on the approved Gemini Flash TTS model; Lite fails closed rather than silently substituting voices. The narrator for unlabelled/prayer units remains Charon. The cache binding contains the actual voices and performance mode so no cross-series voice cache reuse occurs.

## Editorial positioning — موقف من وجهين

Promise: a **grounded everyday situation**, a natural first impression, another plausible reading, an actual test/objection, and a useful distinction or action. Not a generic moral, sad parable, motivational sermon, fake interview or two scripted lecturers.

A is **a credible participant** who has reasons, challenges B, and can be correct. B responds concretely, tests the opposite perspective, and can acknowledge uncertainty. This experimental ZIP route keeps the legacy Podcast safety ceiling of **18 Arabic words per A turn**, and A cannot dominate over 35% of the episode. The structural validator still requires the first A question and an immediate B response. All planning/writing occurs **outside the Runner**, so this branch does not change the Writer's «خارج النص» persona.

Pilot length target: 10–12 minutes (not a new code limit). Preferred beats: specific hook, situation, consequences, first interpretation, counterexample, practical takeaway and earned resolution. Real incidents require sources; invented examples are labelled as illustrative. Fusha, respect for culture/religion, no unsupported expert medical or legal claims.

## Visuals and music

Reuse the existing podcast photos/approved stock/motion/editing engine; 16:9 natural observational scenes, calm authentic colors, edit when meaning changes, not merely on every speaker switch. No reverse, repeated generic stock or visible identifiable faces. New ZIP contains real approved stills and a separate actual approved cover. Optional YouTube Audio Library tracks must match the existing allowlist, have bytes present in ZIP, and pass hash checks; otherwise use `editing.music=none`. Do not fetch music from arbitrary videos.

The new 16:9 intro/outro **prototype** displays the separate Arabic name «على مهل», a restrained circular emblem and original text. It avoids the «نداء اليقظة» and «خارج النص» titles completely. **It is not presented as the owner's previously approved circular logo.** It needs owner visual review/replacement with a licensed/approved actual mark before final production. The prayer image and spoken timing are left unchanged.

## QA gates (before shipping)

1. ZIP imports and script survives text-for-text.
2. Verify A=Iapetus/B=Charon in a real Gemini dual-speaker sample with a valid quota; ensure good Arabic pace, chunk joins, prayer continuity, no dropped words.
3. Verify that legacy `dialogue_qa` remains Orus/Charon with V8 identity and that both channel Short outputs remain unchanged.
4. Decode and watch the real new intro/outro, image coverage, captions, music ducking, and audio-to-picture changes.
5. Review final cover and optional derived Short for «على مهل» channel identity; original primary success stays independent.
6. Claude review/APPROVED and owner-only Telegram import/YouTube publishing. **Do not merge or publish based solely on passing unit tests.**

No provider API calls, recording, final media render, logo approval or production delivery is claimed by this document.
