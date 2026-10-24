# «على مهل ☕» Podcast — opt-in production contract (v1)

## Boundaries

Two independent YouTube channels exist: «نداء اليقظة» (including «خارج النص») and «على مهل ☕» (its own Shorts plus this podcast). There is **no cross-posting, automatic publishing, or engine rewrite**. The new podcast is an **opt-in** approved-ZIP path with `format: "podcast"`, `template: "ala_mahl_dialogue"`. Existing `format: "podcast"`, `template: "dialogue_qa"` continues unchanged. Existing «على مهل» Shorts (`format: "short"`, `template: "ala_mahl"`) continue unchanged.

## Editorial identity — «قبل أن نحكم»

- Focus on one grounded, recognizable real-life dilemma in relationships, family, responsibility, or everyday work.
- Start with a specific question from A, not a motivational slogan or invented fact. A offers a plausible initial judgment, challenges the new angle, and may correct B.
- B replies directly and introduces overlooked details, limits, and practical distinctions. B is not omniscient or a therapist.
- A and B are equal participants, not interviewer and lecturer. No fake banter, padding, forced twists, repeated agreeable phrases, or moralizing.
- Make the listener reconsider the first judgment; the honest conclusion may be that the first judgment was justified.
- Any invented anecdote must be labeled as illustrative; externally verifiable claims require research. Keep the episode useful even with eyes closed.
- Pilot length 10–12 minutes; audio timing controls duration. Natural Modern Standard Arabic. Human editorial work and complete script remain outside the runner.

## Approved ZIP

Follow the existing `docs/production-zip.md` importer contract, except use `template: "ala_mahl_dialogue"` for this opt-in podcast. Exactly the same required `roles: {"A":"listener","B":"host"}` fields are retained for backward-compatible schema. Those strings identify the voice slots, not a requirement that A be an interviewer's assistant.

The importer accepts multi-scene podcast photography/stock, planned text anchors and independent final cover. Stock is optional and must genuinely match the discussed action. Do not force every scene into a fast film montage. Use the existing `editing.music: "none"` or a supported preapproved local YouTube Audio Library track; **never** supply a new unregistered `track_id` or invented stock URL. Claude APPROVED is a manual external review, not proof asserted by text metadata.

## Sound and identity

- Existing «خارج النص» podcast stays `A=Orus`, `B=Charon`.
- Only `ala_mahl_dialogue` uses `A=Iapetus`, `B=Charon`, on the approved Flash TTS model. Never auto-fallback to Lite and never alter voices in the other channel.
- The new podcast has separate 16:9 warm, restrained circular branding; the existing V8 intro/outro and «على مهل» portrait Shorts assets are untouched. The generated card is a **provisional design** and must be checked against the real existing circle logo before release.
- The approved prayer and its position are not changed.
- A first question (complete first turn) → identity intro → prayer → B's response. Identity outro follows all spoken words.
- Character-specific audio cache binding includes questioner voice and performance mode. Inspect TTS records and play an actual A/B sample before using production voice quota on a full episode.

## Release gates

1. Validate one genuine edited ZIP for each existing format and the opt-in podcast.
2. Automated tests pass on PR head, including pronunciation/role-selection mocks, source text, identity isolation, pipeline resume, and import rejection.
3. Run and listen to an actual Gemini dual-speaker Arabic sample, documenting its API response and quota/cost.
4. Render a 16:9 10–12-minute pilot with at least several semantically distinct natural photographs; validate stock rights, captions, music, prayer, outro timing, and derived Short.
5. Compare two channel masters side by side and confirm not one pixel of «خارج النص» branding is present in «على مهل».
6. Owner/Claude review. No merge to main, no YouTube publishing or paid services without explicit owner authorization.

Status: **prototype only** until CI and actual perceptual QA prove the change.
