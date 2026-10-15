"""Owner-written Short ("شورت جاهز"): the owner supplies title, text and scene searches.

The automatic path writes and audits the Short with a small model, which keeps
rejecting the practical-action rule. Here the owner (with his own writing tool)
supplies the finished text, so no planning / script / tone / factuality model call
runs. The pack is turned into exactly the artifacts a fully audited run would have
saved, then the existing resume machinery takes over: voice (Gemini) -> scene
picker -> render. Nothing about voice, picker, owner-pick protection or rendering
is changed.

Telegram format (one message):

    شورت جاهز
    عنوان: ...
    نص 1: ...   (first sentence is the hook)
    نص 2: ...
    نص 3: ...
    مشهد 1: english search ...   (1-3 belong to text 1, 4-5 to text 2, 6-7 to text 3)
    ...
    مشهد 7: english search ...
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

OWNER_PACK_ENV = "CLEAN_V2_OWNER_PACK"
PREFIX = "شورت جاهز"
SCENES_PER_SECTION = (3, 2, 2)
SCENE_COUNT = sum(SCENES_PER_SECTION)
MIN_TOTAL_WORDS = 40
MAX_TOTAL_WORDS = 230
MAX_HOOK_WORDS = 30
MAX_TITLE_CHARS = 120

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_LABEL_RE = re.compile(
    r"(?:(?<=\s)|^)(?P<label>عنوان|نص\s*[1-3١-٣]|مشهد\s*[1-7١-٧])\s*[:：]"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!؟!?])\s+")


class OwnerPackError(ValueError):
    """Message is Arabic and safe to send to the owner as is."""


def has_prefix(text: str) -> bool:
    return str(text or "").lstrip().startswith(PREFIX)


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _label_key(label: str) -> str:
    compact = re.sub(r"\s+", "", label).translate(_DIGITS)
    if compact == "عنوان":
        return "title"
    if compact.startswith("نص"):
        return "text" + compact[2:]
    return "scene" + compact[4:]


def parse_owner_message(text: str) -> dict[str, Any]:
    """Telegram text -> validated pack. Raises OwnerPackError with an Arabic reason."""
    raw = str(text or "").strip()
    if not has_prefix(raw):
        raise OwnerPackError(f"ابدأ الرسالة بعبارة «{PREFIX}».")
    body = raw[raw.index(PREFIX) + len(PREFIX):]
    # Labels are found anywhere after whitespace, so the message survives clients or
    # relays that flatten line breaks.
    marks = list(_LABEL_RE.finditer(body))
    value: dict[str, str] = {}
    for position, match in enumerate(marks):
        key = _label_key(match.group("label"))
        if key in value:
            raise OwnerPackError(f"الحقل مكرر: {match.group('label').strip()}.")
        end = marks[position + 1].start() if position + 1 < len(marks) else len(body)
        value[key] = " ".join(body[match.end():end].split())

    missing = [
        label
        for key, label in (
            [("title", "عنوان")]
            + [(f"text{i}", f"نص {i}") for i in (1, 2, 3)]
            + [(f"scene{i}", f"مشهد {i}") for i in range(1, SCENE_COUNT + 1)]
        )
        if not value.get(key)
    ]
    if missing:
        raise OwnerPackError("ينقص في الرسالة: " + "، ".join(missing) + ".")

    title = value["title"]
    if len(title) > MAX_TITLE_CHARS:
        raise OwnerPackError(f"العنوان أطول من {MAX_TITLE_CHARS} حرفًا.")
    texts = [value[f"text{i}"] for i in (1, 2, 3)]
    total = sum(_words(item) for item in texts)
    if total < MIN_TOTAL_WORDS:
        raise OwnerPackError(f"النص قصير جدًا ({total} كلمة). الحد الأدنى {MIN_TOTAL_WORDS}.")
    if total > MAX_TOTAL_WORDS:
        raise OwnerPackError(f"النص طويل جدًا ({total} كلمة). الحد الأقصى {MAX_TOTAL_WORDS}.")
    hook = _SENTENCE_SPLIT_RE.split(texts[0])[0]
    if _words(hook) > MAX_HOOK_WORDS:
        raise OwnerPackError(
            f"الجملة الأولى (الخطاف) {_words(hook)} كلمة؛ اجعلها أقل من {MAX_HOOK_WORDS + 1} وضع بعدها نقطة."
        )
    scenes = [value[f"scene{i}"] for i in range(1, SCENE_COUNT + 1)]
    for index, query in enumerate(scenes, start=1):
        ascii_letters = len(re.findall(r"[A-Za-z]", query))
        non_space = len(re.findall(r"\S", query))
        if len(query.split()) < 3 or ascii_letters < 0.6 * non_space:
            raise OwnerPackError(f"المشهد {index} يجب أن يكون بحثًا بالإنجليزية من 3 كلمات على الأقل.")
        if len(query) > 220:
            raise OwnerPackError(f"المشهد {index} أطول من 220 حرفًا.")
    if len({" ".join(item.casefold().split()) for item in scenes}) != len(scenes):
        raise OwnerPackError("بعض المشاهد مكررة؛ اجعل كل مشهد مختلفًا.")
    return {"schema_version": 1, "title": title, "texts": texts, "scenes": scenes}


def pack_sha256(pack: Mapping[str, Any]) -> str:
    raw = json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_pack(path: str | os.PathLike[str]) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise OwnerPackError("ملف النص الجاهز غير صالح.")
    texts, scenes = data.get("texts"), data.get("scenes")
    if (
        not str(data.get("title") or "").strip()
        or not isinstance(texts, list) or len(texts) != 3
        or not isinstance(scenes, list) or len(scenes) != SCENE_COUNT
        or not all(str(item).strip() for item in [*texts, *scenes])
    ):
        raise OwnerPackError("ملف النص الجاهز غير مكتمل.")
    return {
        "schema_version": 1,
        "title": " ".join(str(data["title"]).split()),
        "texts": [" ".join(str(item).split()) for item in texts],
        "scenes": [" ".join(str(item).split()) for item in scenes],
    }


def active_pack_path(fmt: str) -> Path | None:
    """The pack file for this run, only for Shorts and only when the workflow set it."""
    value = os.environ.get(OWNER_PACK_ENV, "").strip()
    if not value or str(fmt) != "short":
        return None
    path = Path(value)
    return path if path.is_file() else None


def owner_mode_active(brief: Mapping[str, Any]) -> bool:
    return active_pack_path(str(brief.get("format") or "")) is not None


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in _SENTENCE_SPLIT_RE.split(text) if item.strip()]


def build_artifacts(pack: Mapping[str, Any], brief: Mapping[str, Any], out_dir: Path) -> dict[str, Any]:
    """Write the post-audit artifact set into ``out_dir`` and return the script."""
    from clean_v2.contextual_cta import bind_contextual_cta_to_script
    from clean_v2.contracts import atomic_write_json, validate_plan, validate_script
    from clean_v2.identity_sequence import (
        PRAYER_SENTENCE,
        SHORT_CHANNEL_DEFINITION,
        assert_spoken_identity,
        inject_spoken_identity,
    )
    from clean_v2.visual_story import (
        VISUAL_WORLD_DEFAULT,
        _default_editorial_signals,
        _default_environment_family,
        _default_shot_role,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    title = str(pack["title"])
    texts = [str(item) for item in pack["texts"]]
    scenes = [str(item) for item in pack["scenes"]]

    # Plan: only fields the downstream contracts read. The two queries per section
    # are the section's first and last scene (they must differ).
    sections_plan = []
    start = 0
    for index, count in enumerate(SCENES_PER_SECTION, start=1):
        chunk = scenes[start:start + count]
        start += count
        sections_plan.append({
            "id": f"s{index}",
            "heading": f"القسم {index}",
            "purpose": _sentences(texts[index - 1])[0][:700],
            "visual_query_en": chunk[0],
            "visual_query_alt_en": chunk[-1],
        })
    plan = validate_plan(
        {"title": title, "promise": title, "cta": "", "sections": sections_plan},
        {**brief, "format": "short"},
    )
    plan["owner_authored"] = True
    plan["_visual_diversity_contract"] = "v2_fail_closed"
    plan["_short_visual_diversity_contract"] = "v1_max2"
    plan["_visual_no_face_semantic_contract"] = "v1"
    plan["_visual_semantic_strength_contract"] = "v1_post_hook"
    plan["_visual_identity_contract"] = "navy_gold_v1"
    plan["short_template"] = "micro_story"
    atomic_write_json(out_dir / "plan.json", plan)

    # Script: owner text + the fixed spoken identity, inserted by the same code path.
    script = validate_script(
        {
            "title": title,
            "sections": [{"id": f"s{i}", "narration": texts[i - 1]} for i in (1, 2, 3)],
        },
        plan,
    )
    inject_spoken_identity(script["sections"], fmt="short", opener=SHORT_CHANNEL_DEFINITION, closer="")
    assert_spoken_identity(script["sections"], fmt="short", opener=SHORT_CHANNEL_DEFINITION, closer="")

    identity = {
        "schema_version": 1,
        "source": "clean-v2-short-fixed-identity",
        "status": "pass",
        "reason": "fixed_identity_inserted_after_first_sentence_hook",
        "canonical_opener": SHORT_CHANNEL_DEFINITION,
        "canonical_closer": "",
        "opener": SHORT_CHANNEL_DEFINITION,
        "closer": "",
        "prayer_sentence": PRAYER_SENTENCE,
        "transitions": [],
        "provider_calls_added": 0,
    }
    atomic_write_json(out_dir / "narrative-identity.json", identity)

    # Visual story: 3 hook + 2 body + 2 (last = payoff). Anchors are the owner's own
    # sentences, so beat timing follows the spoken text without any model.
    beats: list[dict[str, Any]] = []
    scene_index = 0
    for section_number, count in enumerate(SCENES_PER_SECTION, start=1):
        owner_sentences = _sentences(texts[section_number - 1]) or [texts[section_number - 1]]
        for within in range(count):
            query = scenes[scene_index]
            scene_index += 1
            if scene_index <= 3:
                role, hold = "hook", "hook_progression"
            elif scene_index == SCENE_COUNT:
                role, hold = "payoff", "payoff_landing"
            else:
                role, hold = "body", None
            anchor = owner_sentences[min(len(owner_sentences) - 1, within * len(owner_sentences) // count)]
            beat = {
                "id": f"b{scene_index}",
                "section_id": f"s{section_number}",
                "viewer_intent": anchor[:300],
                "meaning_target": anchor[:300],
                "semantic_must_have": [query[:120]],
                "semantic_should_avoid": [],
                "shot_intent": query,
                "role": role,
                "stock_query_en": query,
                "stock_query_alt_en": "",
                "display_text_ar": "",
                "source_preference": "stock_motion",
                "shot_role": _default_shot_role(role, within),
                "environment_family": _default_environment_family(query),
                "writer_anchor_ar": anchor,
                **_default_editorial_signals(role),
            }
            if hold:
                beat["hold_reason"] = hold
            beats.append(beat)
    story = {
        "schema_version": 2,
        "visual_world": VISUAL_WORLD_DEFAULT,
        "story_arc": {
            "beginning": _sentences(texts[0])[0][:300],
            "transformation": _sentences(texts[1])[0][:300],
            "arrival": _sentences(texts[2])[-1][:300],
        },
        "beats": beats,
    }
    atomic_write_json(out_dir / "visual-story.json", story)

    atomic_write_json(out_dir / "script.json", script)
    transcript = "\n\n".join(item["narration"] for item in script["sections"])
    (out_dir / "narration.txt").write_text(transcript + "\n", encoding="utf-8")
    atomic_write_json(out_dir / "brief.json", dict(brief))
    bind_contextual_cta_to_script(output_dir=out_dir, brief=brief, plan=plan, script=script)
    atomic_write_json(
        out_dir / "owner-pack.json",
        {**pack, "pack_sha256": pack_sha256(pack), "provider_calls": 0,
         "note": "owner-written text; no planning/script/tone/factuality model call"},
    )
    return script


def light_script_validation(script: Mapping[str, Any]) -> None:
    """Structure only. Editorial shape (hook length, action marker) is the owner's call."""
    sections = script.get("sections")
    if not isinstance(sections, list) or len(sections) != 3:
        raise OwnerPackError("النص الجاهز يجب أن يحتوي ثلاثة أقسام.")
    if any(len(str(item.get("narration") or "").strip()) < 20 for item in sections):
        raise OwnerPackError("قسم من النص الجاهز فارغ أو قصير جدًا.")
