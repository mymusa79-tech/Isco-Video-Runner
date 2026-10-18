# Approved ZIP production (schema_version 1)

Telegram: **🎬 إنتاج من ملف جاهز** → send a ZIP as a document → read its summary → **✅ تأكيد الإنتاج**.
Import alone never dispatches production. The old TXT and typed-message routes remain supported.
The approval metadata records the supplied claim of Claude review; it cannot authenticate an external review.

## Archive

Exactly one `production.json` at the root, plus static PNG/JPEG/WebP files under `assets/`.
Names: `assets/scene_01.png`, for example (ASCII letters, digits, `_` and `-`; no subdirectories).
No scripts, SVG, audio/video, URLs, symlinks, encrypted entries or duplicate names.
18 MiB archive, 96 MiB expanded total, 65 entries, 12 MiB/image, 300 KiB JSON, 25 million pixels/image.
Images are checked on receipt and the exact archive SHA-256 is checked again at production.
Send missing or revised assets as a new complete ZIP and confirm it again.

## Manifest fields

Unknown fields are rejected. Nothing in a ZIP can change the established model, voice IDs or format identity.
Only the performance directions enter TTS style metadata; they are never appended to spoken text.

| Field | Contract |
|---|---|
| `schema_version` | Integer `1` |
| `format` | `short`, `film`, `podcast` |
| `template` | Short: `why_reframe`, `inner_dialogue`, `micro_story`, `quote_reflection`, `ala_mahl`. Film: `direct_cinematic`, `question_answer`, `inner_dialogue`, `problem_reveal_solution`, `story_analysis`, `paradox`, `hypothesis_test`, `connected_list`. Podcast: `dialogue_qa`. |
| `title` | Nonempty string, maximum 120 characters |
| `approval` | `{"status":"approved","reviewer":"Claude"}` |
| `script.sections` | Exactly 3 Short sections, exactly 5 Film sections, 2–5 Podcast sections; IDs `s1`, `s2`, … |
| Non-Podcast section | `{"id":"s1","text":"Final approved narration."}` (at least 20 characters) |
| Podcast section | `{"id":"s1","turns":[{"speaker":"A","text":"Question?"},{"speaker":"B","text":"Answer."}]}` |
| `roles` | Podcast only, required: `{"A":"listener","B":"host"}`. Existing A=Orus, B=Charon mapping remains. |
| `performance` | Required `direction` string (may be empty); optional `A`/`B` style directions for Podcast; each ≤800 characters |
| `scenes` | Ordered list, 1–30 entries, every section covered; Ala Mahl: 2–4 distinct portrait images (old 5–6-image ZIPs remain accepted) |
| Scene | `id` (unique lowercase ASCII identifier), `section_id`, `asset`, `start_text`, `motion`, `transition` |
| `stock` (optional) | Nidaa Short and Film only; never Podcast or Ala Mahl. `{"query_en":"person walking on sidewalk","query_alt_en":"…","must_show":"…"}`: English 2–9 word query; no other fields, no URLs. At most 3 stock scenes in a Short and 4 in a Film. The scene's required `asset` is the approved fallback image: it is used whenever the client cannot admit a real stock clip (no result, provider/quota/key failure, or descriptive metadata that does not match the query). No AI image is generated in its place. The report records `stock_fallback_reason` per scene. |
| `start_text` | Exact unique text inside that section, with speaker labels omitted. First scene of every section starts with its first spoken words. Subsequent anchors must be in text order. |
| `motion` | `slow_pan`, `slow_zoom`, `hold` |
| `transition` | `cut`, `dissolve` into that scene. Measured identity boundaries stay cuts. |
| `editing` | `{"music":"none"}` (no background music in v1 ZIP mode) |
| `captions` | `{"font":"Cairo Bold","max_lines":2,"enabled":true}`; uses shared format caption owners |
| `cover` | `{"asset":null,"text":"Short cover text"}` or approved image path. A supplied image is used as the cover; its pixels must already contain any desired text. With null, the existing local cover generator is used. |
| `publishing` | `title`, `description`, `hashtags` (list of up to 15 `#tags`, no spaces). Preserved in the manual publishing package. |
| `derivative` | Optional for Film/Podcast: `{"start_text":"Exact beginning sentence.","end_text":"Exact ending sentence."}`. See derivative contract below. |

Do not write the prayer, channel definition or fixed identity text in `script`: the existing identity owner adds it once.
Write any desired spoken CTA in the approved narration at its intended location. ZIP mode does not insert or relocate a CTA.
No planning, script-writing, tone/factuality rewrite or stock-replacement model is called for this approved input.

## Ala Mahl

`format="short"`, `template="ala_mahl"`. Use 2–4 distinct expressive 9:16 story images.
Pictures follow meaningful story turns, not every sentence. Old 5–6-image ZIPs remain readable; do not author new ones that way.
The first picture starts at the beginning of `s1`. A section without a new picture continues the preceding one;
the first new picture in a covered section still anchors to its first words. The same image can therefore span
several sections without being listed twice or restarting its movement. All three script sections remain required.
`hold` is intentional stillness; `slow_pan` and `slow_zoom` are optional authored choices, never added on top.
Their content and order are the author's responsibility; the importer checks their existence, type, dimensions and linkage.
The pipeline never generates images, searches stock or replaces them. Audio determines the final shot windows.
Authored scene anchors inside an audio section use the existing text-proportional timing (not word-level forced alignment).
Actual mastered duration must be 25–120 seconds. No speed-up, padding to meet the limit or second TTS pass is attempted.
Keeps the usual Short voice and trusted prayer picture, omits the channel definition/logo intro,
and ends with the owner's approved circular **على مهل** logo in `ala_mahl_outro.mp4`.
This is the existing Nidaa Short outro with only its logo replaced; its subscribe animation is unchanged.
The complete 67-frame / 30 fps clip occupies 2.233 seconds of terminal silence, after the final spoken word.
Only that local silence grows from the former 1.25-second signature window; narration and provider calls are unchanged.
The 25–120-second mastered-duration limit still includes the terminal silence. Existing saved audio is never regenerated
to update the outro: older resumes retain their measured terminal window. No separate intro or spoken CTA is added.
Approved ZIP pictures bypass reference matching, the master palette LUT and cinematic finishing.
Only sizing/cropping, authored movement, transitions and separate caption/identity layers affect the rendered composition;
the original asset bytes are retained. Non-ZIP Podcast and photographic-still episodes also bypass the global palette finish.
Stock-only Nidaa episodes keep their existing finish and source-order policy.

## Identity

Normal Nidaa Short: hook → natural 0.25s breath → prayer/picture → spoken channel definition with the logo
**only while that definition is spoken** → topic → Short outro. There is no separate silent logo intro.
Film and Podcast V8 keep their existing identity. Approved narration is preserved except for host-owned identity insertion
and whitespace normalization; no authored word is added, substituted or removed.

## Derivative contract

One optional continuous excerpt comes from the successful final video, never a second voice or planning pass.
Use exact complete sentences/turns at both boundaries and keep the question and answer together in Podcast.
Write `start_text` and `end_text` without A:/B: labels. The initial voice pass may expose these boundaries
as separate measured chunks of the same approved narration. No passage is read a second time for extraction.
For Podcast, choose complete turn boundaries so the excerpt preserves each speaker's meaning.
If a supplied span cannot be mapped safely to the measured saved audio, the derivative is skipped with a reason.
The parent video remains successful and is delivered independently of this optional step.

The source video is never overwritten. `primary-success.json` freezes its SUCCESS and input hashes before extraction.
`derivative-status.json` records independent pass/failed/skipped status and a reason; optional cover/metadata packaging
failures are isolated too. A failed derivative never changes `run-manifest.json` from pass to failed.
The main download is delivered first, and failed/skipped derivatives produce a warning with the reason.

The parent Release also carries `derivative-source.zip` with its accepted mastered audio, script, timing,
caption events and hashes. The original `final.mp4` remains a separate asset. The Telegram **♻️ إعادة المشتق فقط**
button selects the last Long/Podcast with those frozen assets and dispatches `clean-v2-derivative-only.yml`.
That workflow has no voice credentials, no Engine checkout and no primary pipeline command.
It downloads the existing original and source, verifies their hashes, and extracts/delivers only the derivative.
It does not rebuild the original publishing package or alter the original video.

Local equivalent after placing the original `final.mp4` in an otherwise empty directory:

```sh
python -m clean_v2.derived_short --output /path/to/retry --restore-source /path/to/derivative-source.zip
```

Without a supplied candidate, the extractor tries one complete existing measured topic/promo unit (7–60 seconds).
It skips the derivative when no complete unit is safe. It does not invent another script or image.

See `docs/examples/production.ala_mahl.json` for an executable example of the actual schema.
