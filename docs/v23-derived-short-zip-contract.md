# V23 — Derived Short integration notes

## Existing ZIP contract (verified in clean_v2/production_package.py)

For Film and Podcast only, `production.json` already accepts an optional `derivative` object with exactly two keys:

```json
"derivative": {
  "start_text": "Exact complete opening sentence from the approved script.",
  "end_text": "Exact complete closing sentence from the approved script."
}
```

Use real Arabic sentences from the final script, not these English placeholders. Both anchors must be unique, complete sentences and align with measured voice units. For Podcast, preserve complete dialogue turn boundaries. A rejected optional derivative must not invalidate the parent.

`clean_v2/derived_short.py` already implements offline extraction up to 60 seconds using measured audio, a caption-free master when available, and one caption track. This is distinct from the older `_run_podcast_derived_short_lite` path with a 30-second ceiling.

## Editorial choice

ChatGPT reviews three candidates and writes the strongest genuine contiguous A-question/B-answer excerpt into the existing optional derivative field, after Claude approval and schema validation. If no candidate is suitable, omit the field and let the existing local selector run. Never invent or reorder spoken words.

## CTA — not yet implemented

A visual invitation in the final five seconds is a **separate feature**. Do not extend, mute, or overwrite the spoken ending until a renderer-level timing and QC regression is implemented. The user can add YouTube's related-video link manually.

## Scope of PR #1169

This PR only changes the fallback podcast excerpt scorer to prefer a real adjacent A/B exchange. It does not alter the ZIP schema, derivative renderer, TTS, source video, or the parent episode. Do not merge before CI and review.
