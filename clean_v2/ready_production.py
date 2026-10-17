"""Adapt a validated ZIP manifest to the existing owner-script resume artifacts."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import atomic_write_json, validate_plan, validate_script
from .identity_sequence import PRAYER_SENTENCE, assert_spoken_identity, inject_spoken_identity, _first_sentence
from .production_package import section_text, spoken_text, validate_manifest


def document(output_dir: Path) -> dict:
    path = Path(output_dir) / "production.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def build_story(doc: Mapping[str, Any]) -> dict:
    from .visual_story import CHANNEL_VISUAL_IDENTITY, _default_editorial_signals, _default_shot_role

    beats = []
    for section in doc["script"]["sections"]:
        sid = section["id"]
        text = spoken_text(section_text(section))
        hook = _first_sentence(text) if sid == "s1" else ""
        scenes = [s for s in doc["scenes"] if s["section_id"] == sid]
        if beats and (not scenes or text.index(scenes[0]["start_text"]) > 0):
            # Section boundaries are audio bookkeeping, not picture changes.
            # The renderer joins these continuations into one uninterrupted shot.
            previous = beats[-1]
            scenes = [{"id": f"__continuation_{sid}", "section_id": sid,
                       "asset": previous["owner_asset"], "start_text": text[:300],
                       "motion": previous["owner_motion"], "transition": "cut"}, *scenes]
        expanded = [dict(s) for s in scenes]
        if hook and text[len(hook):].strip():
            hook_scenes = [s for s in scenes if text.index(s["start_text"]) < len(hook)]
            body_scenes = [s for s in scenes if s not in hook_scenes]
            remainder = text[len(hook):].strip()
            # A picture covering the hook may also cover the next phrase. This
            # virtual hold uses that SAME approved image, not another source.
            if not body_scenes or text.index(body_scenes[0]["start_text"]) > len(hook) + 1:
                shared = dict(hook_scenes[-1])
                shared.update(id=shared["id"] + "__topic", start_text=remainder[:300])
                expanded.insert(len(hook_scenes), shared)
        for within, scene in enumerate(expanded):
            role = "hook" if hook and text.index(scene["start_text"]) < len(hook) else "body"
            query = f"approved owner illustration scene {len(beats) + 1}"
            beats.append({
                "id": scene["id"], "section_id": sid, "role": role,
                "viewer_intent": scene["start_text"], "meaning_target": scene["start_text"],
                "shot_intent": query, "stock_query_en": query, "source_preference": "stock_motion",
                "semantic_must_have": [query], "semantic_should_avoid": [], "display_text_ar": "",
                "writer_anchor_ar": scene["start_text"], "shot_role": _default_shot_role(role, within),
                "environment_family": "other", **_default_editorial_signals(role),
                "owner_asset": scene["asset"], "owner_motion": scene["motion"], "owner_transition": scene["transition"],
            })
    beats[-1]["role"] = "payoff"
    return {"schema_version": 2, "visual_world": CHANNEL_VISUAL_IDENTITY,
            "story_arc": {"beginning": doc["title"], "transformation": doc["title"], "arrival": doc["title"]},
            "beats": beats}


def build_artifacts(pack: Mapping[str, Any], brief: Mapping[str, Any], out_dir: Path) -> dict:
    from .contextual_cta import CTA_CONTRACT_VERSION, CtaBinding, CtaMode, write_cta_report
    from .owner_film import _brand_signature

    doc = validate_manifest(pack["production"], set(pack["asset_sha256"]))
    fmt = doc["format"]
    out_dir.mkdir(parents=True, exist_ok=True)
    sections = [{"id": s["id"], "narration": section_text(s)} for s in doc["script"]["sections"]]
    plan = validate_plan({
        "title": doc["title"], "promise": doc["title"], "cta": "",
        "narrative_format": doc["template"] if fmt != "short" else "",
        "cover_text": doc["cover"]["text"],
        "sections": [{"id": s["id"], "heading": f"الجزء {i}", "purpose": doc["title"],
                      "visual_query_en": f"approved owner illustration section {i}"}
                     for i, s in enumerate(sections, 1)],
    }, brief, owner_approved=True)
    plan.update(owner_authored=True, ready_package=True)
    if fmt == "short":
        plan["short_template"] = doc["template"]
    script = validate_script({"title": doc["title"], "sections": sections}, plan)
    opener, closer = _brand_signature() if fmt == "film" else ("", "")
    inject_spoken_identity(script["sections"], fmt=fmt, opener=opener, closer=closer)
    if doc["template"] == "ala_mahl":
        from .identity_sequence import SHORT_CHANNEL_DEFINITION
        script["sections"][0]["narration"] = script["sections"][0]["narration"].replace(SHORT_CHANNEL_DEFINITION + " ", "")
    else:
        assert_spoken_identity(script["sections"], fmt=fmt, opener=opener, closer=closer)
    # There is no CTA relocation/rewriting in ZIP mode. A CTA can be part of the
    # approved text, at the location the author chose.
    write_cta_report(out_dir / "cta-plan.json", CtaBinding(
        CTA_CONTRACT_VERSION, CtaMode.NONE, None, "", "", "", False, "approved_zip_no_text_insertion"
    ), None)
    atomic_write_json(out_dir / "production.json", copy.deepcopy(doc))
    atomic_write_json(out_dir / "plan.json", plan)
    atomic_write_json(out_dir / "script.json", script)
    atomic_write_json(out_dir / "visual-story.json", build_story(doc))
    atomic_write_json(out_dir / "brief.json", dict(brief))
    atomic_write_json(out_dir / "narrative-identity.json", {
        "schema_version": 1, "source": "approved-zip-existing-format-identity", "status": "pass",
        "opener": opener, "closer": closer, "prayer_sentence": PRAYER_SENTENCE,
        "transitions": [], "provider_calls_added": 0,
    })
    (out_dir / "narration.txt").write_text("\n\n".join(s["narration"] for s in script["sections"]) + "\n", encoding="utf-8")
    return script
