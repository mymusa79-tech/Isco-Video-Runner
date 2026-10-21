# Editorial type selection authority — Nidaa Production (2026-10)

## Non-negotiable owner decision

**ChatGPT is the editor and planner, before any script is written.** The producer does not
select a convenient template after writing, infer it from the already generated script,
or rotate styles just to make counts look varied.

1. Read the approved topic, audience payoff, evidence, emotional direction, and actual
   medium (Nidaa Short, Film/long, Outside the Text Podcast, Ala Mahl).
2. **Choose the single most suitable narrative template first**, on what most clearly
   communicates that particular idea, not on recent-format balancing alone.
3. Commit to that template before drafting. Plan hook, logical transitions, spoken
   performance, shot intentions, visual continuity, real-stock candidates, final payoff,
   and the ending *for that template*. Then write and audit the complete script.
4. Freeze the approved text; build `production.json` with its chosen `template`, 3 short
   sections or 5 Film sections, anchored scenes, and actual visual fallback image assets.
5. Check that the image choices and stock queries illustrate what the script says; real
   action must not be replaced with a symbolic unrelated picture. Use authorized Pexels,
   Pixabay or Coverr references when available. Never invent a clip ID or claim a download.
6. Supply draft ZIP for Claude review. **Do not label the archive Claude-approved until
   Claude actually approves**. Only then complete the approval field required by the
   importer. Owner imports via Telegram and manually publishes; no automatic YouTube upload.

## One narrative type per production, chosen BEFORE writing

### Nidaa Shorts — exactly four
- `why_reframe`: expose a concrete wrong frame → opposing observation → corrected
  frame → one useful action. Favor visual contrasts and purposeful hard cuts.
- `inner_dialogue`: an internal tension → hesitation → honest self-correction →
  changed action. Intimate, consistent scene, restrained motion and breathing room.
- `micro_story`: observable person/place/event → complication → decisive action →
  consequence. **Real causal action and prop/location continuity, not a slideshow
  of disconnected illustrations**. Each cut advances the story.
- `quote_reflection`: verified quotation actually in the topic → brief considered
  pause → grounded example → useful meaning. No invented attribution or generic aphorism.

### Long Film — exactly eight
- `direct_cinematic`: immediate tension → developing evidence/action → resolution.
- `question_answer`: real question → new visual evidence → next question → deeper answer.
- `inner_dialogue`: inner resistance → action/inaction contrast → honest correction.
- `problem_reveal_solution`: specific problem → mechanism → intervention → observed result.
- `story_analysis`: continuous event/character/world → causal turn → analysis → implication.
- `paradox`: apparently opposing truths → grounded comparison → resolution.
- `hypothesis_test`: testable claim → fair comparison/evidence → qualified conclusion.
- `connected_list`: cumulative connected reasons/steps → different observed states.

These names are not a requirement to alternate formats. Use the one with the strongest
fit to the specific topic and its evidence. A strong style may recur on different topics.

### Other series
- `podcast / dialogue_qa`: preserve the established listener A / respondent B style,
  single topic-led picture by default, and sparse text. Do not rotate Film templates here.
- `ala_mahl`: compact causal short story, its existing format, no long variant.

## Import contract and authority

- The approved ZIP's top-level `format` and `template` are **authoritative**.
  `production_package.validate_manifest` only accepts the existing enumerations.
- `ready_production.build_artifacts` copies the ZIP template to the plan and Short
  template field. `timeline_first` carries it to the renderer.
- `owner_art._acquire_ready` uses the exact editor-pinned images, motion and transitions.
  A requested stock clip is optional, admitted only when genuinely relevant, else
  approved image fallback. **Do not re-plan inside the importer**.
- The outside editor authors `scenes[].transition` and `scenes[].motion`: these have
  higher priority than any default short pacing variation.
- Format-aware performance directions live in `performance.direction`. Do not replace
  approved narration or individual speaker voices to express variety.
- Do not introduce schema fields such as `narrative_choice_reason`, `status` or
  `import_schema_verified` into `production.json`. Editorial analysis belongs
  outside the importer JSON.

## Release acceptance

Compare the rendered Short, Film and Podcast with real video/audio before declaring
ready. Look for: coherent stock action, consistent objects and people, locally matching
luminance/white balance/saturation, visible captions on phones, real template-specific
story progression, no blue/empty pre-outro card, final word complete before the outro,
and no forced music or surprise logo on approved scenes. Export success alone does not
certify editorial quality.

No new engine, paid AI providers, unattended publication, automatic merge or hidden
artificial changes to the approved script.
