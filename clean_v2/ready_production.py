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
    # Keep old one-art podcast ZIPs static, but honor new multi-shot editorial plans.
    podcast_legacy_still = (doc["format"] == "podcast"
                            and len({scene["asset"] for scene in doc["scenes"]}) == 1
                            and not any(scene.get("stock") for scene in doc["scenes"]))
    three_link_scene_sources = doc.get("stock_policy") == "three_urls_per_scene"
    for section in doc["script"]["sections"]:
        sid = section["id"]
        text = spoken_text(section_text(section))
        # A podcast starts with one complete listener turn, not the first dot.
        turns = section.get("turns") or []
        hook = (str(turns[0]["text"]).strip() if doc["format"] == "podcast" and sid == "s1"
                and turns and turns[0].get("speaker") == "A"
                else (_first_sentence(text) if sid == "s1" else ""))
        scenes = [s for s in doc["scenes"] if s["section_id"] == sid]
        if not scenes and doc["template"] == "ala_mahl" and beats:
            # Section boundaries are audio bookkeeping, not picture changes.
            # The renderer joins these continuations into one uninterrupted shot.
            previous = beats[-1]
            scenes = [{"id": f"__continuation_{sid}", "section_id": sid,
                       "asset": previous["owner_asset"], "start_text": text[:300],
                       "motion": previous["owner_motion"], "transition": "cut"}]
        expanded = [dict(s) for s in scenes]
        # For the new three-link contract each authored scene owns one selected
        # stock source. Never fabricate a second __topic picture beat after the
        # intro hook: that would show fallback art even when stock succeeded.
        # The original scene covers its narration span until the next anchor.
        if hook and text[len(hook):].strip() and not three_link_scene_sources:
            hook_scenes = [s for s in scenes if text.index(s["start_text"]) < len(hook)]
            body_scenes = [s for s in scenes if s not in hook_scenes]
            remainder = text[len(hook):].strip()
            # A picture covering the hook may also cover the next phrase. This
            # virtual hold uses that SAME approved image, not another source.
            if not body_scenes or text.index(body_scenes[0]["start_text"]) > len(hook) + 1:
                shared = dict(hook_scenes[-1])
                shared.update(id=shared["id"] + "__topic", start_text=remainder[:300])
                shared.pop("stock", None)  # the virtual hold reuses the approved image, never a second stock search
                shared.pop("board", None)  # nor a second board; the hook scene is never a board anyway
                expanded.insert(len(hook_scenes), shared)
        for within, scene in enumerate(expanded):
            role = "hook" if hook and text.index(scene["start_text"]) < len(hook) else "body"
            query = f"approved owner illustration scene {len(beats) + 1}"
            intent = query
            stock = scene.get("stock") if isinstance(scene.get("stock"), dict) else None
            must_have = [query]
            if stock:
                # Real stock is only a request: `owner_asset` below stays the approved
                # image that is used whenever no clip is admitted.
                # A pinned scene (stock.url) may omit the search words; the link itself is the choice.
                query = str(stock.get("query_en") or stock.get("must_show") or "editor pinned stock footage").strip()
                intent, must_have = query, [query]
                # The owner's own `must_show` (what the clip has to show, and what it must not)
                # now reaches the existing visual review instead of being ignored.
                must_show = str(stock.get("must_show") or "").strip()
                if must_show:
                    intent = f"{query}. Must show: {must_show}"
                    must_have = [query, must_show]
            board = scene.get("board") if isinstance(scene.get("board"), dict) else None
            board_text = ""
            if board:
                begin = text.index(scene["start_text"])
                later = [text.index(x["start_text"]) for x in expanded[within + 1:] if text.index(x["start_text"]) > begin]
                board_text = text[begin:later[0] if later else len(text)]
            beats.append({
                "id": scene["id"], "section_id": sid, "role": role,
                "viewer_intent": scene["start_text"], "meaning_target": scene["start_text"],
                "shot_intent": intent, "stock_query_en": query, "source_preference": "stock_motion",
                "semantic_must_have": must_have, "semantic_should_avoid": [], "display_text_ar": "",
                "writer_anchor_ar": scene["start_text"], "shot_role": _default_shot_role(role, within),
                "environment_family": "other", **_default_editorial_signals(role),
                # Legacy single-art podcasts remain still. Multi-shot podcasts,
                # films and Shorts honor the approved edit and transitions.
                "owner_asset": scene["asset"],
                "owner_motion": "hold" if podcast_legacy_still else scene["motion"],
                "owner_transition": "cut" if podcast_legacy_still else scene["transition"],
                **({"owner_stock": {k: (copy.deepcopy(v) if k == "urls" else str(v).strip())
                                   for k, v in stock.items()}} if stock else {}),
                # Dialogue Podcast stays on its one approved background even if
                # an older ZIP contains explainer-board metadata.
                **({"owner_board": {"spec": copy.deepcopy(board), "text": board_text, "section_words": len(text.split())}}
                   if board and doc["format"] != "podcast" else {}),
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
