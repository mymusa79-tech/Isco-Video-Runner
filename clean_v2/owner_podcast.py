"""Owner-written Podcast ("بودكاست جاهز"): the owner supplies the whole dialogue.

Same idea as the owner Short: text and planning come from outside (ChatGPT + the
owner), so no planning / script / tone / factuality model call runs. The dialogue is
turned into exactly the artifacts an audited run would have saved and the existing
resume door takes over (Gemini voice -> render). Images the owner designed are used as
the visuals (see owner_art.py); nothing about the voices or the fixed A/B format moves.

Telegram format (several messages are allowed, see the control script):

    بودكاست جاهز
    عنوان: ...
    غلاف: كلمات قصيرة (اختياري)
    دعوة: جملة واحدة في آخر الحلقة (اختياري)
    A: سؤال المستمع؟
    B: الجواب.
    A: ...
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .owner_pack import OwnerPackError

PREFIX = "بودكاست جاهز"
FORMAT = "podcast"
MIN_SECTIONS, MAX_SECTIONS = 2, 5
TARGET_WORDS_PER_SECTION = 330
MIN_WORDS = 250  # below this it is not an episode at all; the 8-minute target is only warned about
RECOMMENDED_WORDS = 840  # 8 minutes at the channel's 105 words per minute
MAX_TITLE_CHARS = 120
DEFAULT_CTA = "شاركنا رأيك في التعليقات."

_LABEL_RE = re.compile(r"(?:(?<=\s)|^)(?P<label>عنوان|غلاف|دعوة|[AB])\s*[:：]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!؟!?])\s+")


def has_prefix(text: str) -> bool:
    return str(text or "").lstrip().startswith(PREFIX)


def _words(text: str) -> int:
    return len(str(text).split())


def _arabic_reason(code: str) -> str:
    mapping = {
        "hook_must_be_listener_A": "أول سطر يجب أن يكون A: وهو سؤال المستمع.",
        "hook_must_be_question": "أول سؤال (A) يجب أن يحتوي علامة استفهام ؟",
        "hook_requires_immediate_charon_answer": "بعد أول سؤال A: يجب أن يأتي جواب B: مباشرة.",
        "question_requires_immediate_answer": "كل سؤال A: يجب أن يتبعه جواب B: مباشرة.",
        "questioner_dominates_episode": "كلام A أكثر من 35% من الحلقة؛ اختصر أسئلة A أو أطل أجوبة B.",
        "question_too_long": "سؤال A: أطول من 20 كلمة؛ اختصره.",
    }
    for key, value in mapping.items():
        if key in code:
            return value
    return "ترتيب الحوار غير صالح (A سؤال قصير ثم B جواب)."


def parse_podcast_message(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not has_prefix(raw):
        raise OwnerPackError(f"ابدأ الرسالة بعبارة «{PREFIX}».")
    body = raw[raw.index(PREFIX) + len(PREFIX):]
    marks = list(_LABEL_RE.finditer(body))
    fields: dict[str, str] = {}
    turns: list[tuple[str, str]] = []
    for position, match in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else len(body)
        value = " ".join(body[match.end():end].split())
        label = match.group("label")
        if label in ("A", "B"):
            if not value:
                raise OwnerPackError(f"سطر {label}: فارغ.")
            turns.append((label, value))
            continue
        if label in fields:
            raise OwnerPackError(f"الحقل مكرر: {label}.")
        fields[label] = value
    title = fields.get("عنوان", "")
    if not title:
        raise OwnerPackError("ينقص في الرسالة: عنوان.")
    if len(title) > MAX_TITLE_CHARS:
        raise OwnerPackError(f"العنوان أطول من {MAX_TITLE_CHARS} حرفًا.")
    if not turns:
        raise OwnerPackError("لم أجد حوارًا. اكتب الأدوار هكذا: A: سؤال؟ ثم B: جواب.")
    total = sum(_words(spoken) for _, spoken in turns)
    if total < MIN_WORDS:
        raise OwnerPackError(f"الحوار قصير جدًا ({total} كلمة).")

    # The same listener-proxy validator the automatic path uses, so a bad dialogue is
    # explained now instead of after a voice call.
    try:
        from clean_v2.pipeline import _validate_podcast_listener_proxy_script
    except Exception:  # pragma: no cover - the run validates again anyway
        _validate_podcast_listener_proxy_script = None
    if _validate_podcast_listener_proxy_script is not None:
        narration = " ".join(f"{speaker}: {spoken}" for speaker, spoken in turns)
        try:
            _validate_podcast_listener_proxy_script({"sections": [{"narration": narration}]})
        except RuntimeError as exc:
            raise OwnerPackError(_arabic_reason(str(exc))) from exc

    cover_text = fields.get("غلاف", "")
    if cover_text and not 1 <= len(cover_text.split()) <= 6:
        raise OwnerPackError("نص الغلاف من كلمة إلى 6 كلمات.")
    cta = fields.get("دعوة", "") or DEFAULT_CTA
    from clean_v2.contextual_cta import CtaMode, MAX_SPOKEN_WORDS, infer_cta_mode

    mode, _reason = infer_cta_mode(cta)
    if mode == CtaMode.NONE or len(cta.split()) > MAX_SPOKEN_WORDS:
        raise OwnerPackError(
            f"الدعوة يجب أن تكون جملة واحدة قصيرة بطلب واحد (مثل: {DEFAULT_CTA})."
        )
    return {
        "schema_version": 1,
        "format": FORMAT,
        "title": title,
        "cover_text": cover_text,
        "cta": cta,
        "turns": [[speaker, spoken] for speaker, spoken in turns],
    }


def section_groups(turns: list[list[str]]) -> list[list[list[str]]]:
    """Split at A->B pair boundaries into 2-5 sections of similar length."""
    pairs: list[list[list[str]]] = []
    for speaker, spoken in turns:
        if speaker == "A" or not pairs:
            pairs.append([[speaker, spoken]])
        else:
            pairs[-1].append([speaker, spoken])
    total = sum(_words(spoken) for _, spoken in turns)
    count = max(MIN_SECTIONS, min(MAX_SECTIONS, round(total / TARGET_WORDS_PER_SECTION)))
    count = min(count, len(pairs))
    if count < MIN_SECTIONS:
        raise OwnerPackError("الحوار يحتاج على الأقل سؤالين A: لتقسيمه إلى قسمين.")
    goal = total / count
    groups: list[list[list[str]]] = [[]]
    running = 0
    for index, pair in enumerate(pairs):
        pairs_left = len(pairs) - index
        sections_left = count - len(groups)
        if groups[-1] and running >= goal and sections_left > 0 and pairs_left >= sections_left:
            groups.append([])
            running = 0
        groups[-1].extend(pair)
        running += sum(_words(spoken) for _, spoken in pair)
    return groups


def estimated_minutes(pack: Mapping[str, Any]) -> float:
    return sum(_words(spoken) for _, spoken in pack["turns"]) / 105.0


def _two_beat_story(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Two beats per section: the hook section needs a hook beat AND a later topic beat,
    and a second beat gives a long section a second move over the same designed image."""
    from clean_v2.visual_story import (
        _default_editorial_signals,
        _default_environment_family,
        _default_shot_role,
        fallback_visual_story,
    )

    story = fallback_visual_story(plan)
    sections = [s for s in plan["sections"] if isinstance(s, Mapping)]
    beats: list[dict[str, Any]] = []
    total = len(sections) * 2
    for section_index, section in enumerate(sections):
        for within in range(2):
            position = section_index * 2 + within
            role = "hook" if position == 0 else ("payoff" if position == total - 1 else "body")
            query = f"owner designed illustration part {section_index + 1} view {within + 1}"
            purpose = str(section.get("purpose") or "").strip()
            beats.append({
                "id": f"b{position + 1}",
                "section_id": str(section["id"]),
                "viewer_intent": f"{purpose}؛ المرحلة {position + 1}".strip("؛ "),
                "meaning_target": purpose or query,
                "semantic_must_have": [query],
                "semantic_should_avoid": ["generic mood-only productivity imagery"],
                "shot_intent": query,
                "role": role,
                "stock_query_en": query,
                "display_text_ar": "",
                "source_preference": "stock_motion",
                "shot_role": _default_shot_role(role, within),
                "environment_family": _default_environment_family(query),
                **_default_editorial_signals(role),
            })
    story["beats"] = beats
    return story


def build_artifacts(pack: Mapping[str, Any], brief: Mapping[str, Any], out_dir: Path) -> dict[str, Any]:
    from clean_v2.contextual_cta import bind_contextual_cta_to_script
    from clean_v2.contracts import atomic_write_json, validate_plan, validate_script
    from clean_v2.identity_sequence import assert_spoken_identity, inject_spoken_identity
    from clean_v2.visual_story import fallback_visual_story, validate_visual_story

    out_dir.mkdir(parents=True, exist_ok=True)
    title = str(pack["title"])
    groups = section_groups([list(item) for item in pack["turns"]])
    narrations = [
        " ".join(f"{speaker}: {spoken}" for speaker, spoken in group) for group in groups
    ]
    sections_plan = []
    for index, group in enumerate(groups, start=1):
        answer = next((spoken for speaker, spoken in group if speaker == "B"), group[0][1])
        sections_plan.append({
            "id": f"s{index}",
            "heading": f"الجزء {index}",
            "purpose": _SENTENCE_SPLIT_RE.split(answer)[0][:600],
            "visual_query_en": f"owner designed illustration part {index}",
        })
    plan_input: dict[str, Any] = {
        "title": title,
        "promise": title,
        "cta": str(pack["cta"]),
        "narrative_format": "dialogue_qa",
        "sections": sections_plan,
    }
    if pack.get("cover_text"):
        plan_input["cover_text"] = pack["cover_text"]
    plan = validate_plan(plan_input, {**brief, "format": FORMAT})
    plan["owner_authored"] = True
    plan["_visual_identity_contract"] = "navy_gold_v1"
    atomic_write_json(out_dir / "plan.json", plan)

    script = validate_script(
        {"title": title, "sections": [{"id": f"s{i}", "narration": n} for i, n in enumerate(narrations, 1)]},
        plan,
    )
    inject_spoken_identity(script["sections"], fmt=FORMAT)
    assert_spoken_identity(script["sections"], fmt=FORMAT)
    atomic_write_json(
        out_dir / "narrative-identity.json",
        {
            "schema_version": 1,
            "source": "clean-v2-podcast-fixed-identity",
            "status": "pass",
            "reason": "owner_dialogue_with_fixed_prayer_bridge",
            "opener": "",
            "closer": "",
            "transitions": [],
            "provider_calls_added": 0,
        },
    )
    story = validate_visual_story(_two_beat_story(plan), plan, from_checkpoint=True)
    atomic_write_json(out_dir / "visual-story.json", story)
    atomic_write_json(out_dir / "brief.json", dict(brief))
    # The spoken CTA is inserted into the script in place, so bind it BEFORE saving the
    # script: the saved script is what the voice reads and what the CTA plan describes.
    bind_contextual_cta_to_script(output_dir=out_dir, brief=brief, plan=plan, script=script)
    atomic_write_json(out_dir / "script.json", script)
    (out_dir / "narration.txt").write_text(
        "\n\n".join(item["narration"] for item in script["sections"]) + "\n", encoding="utf-8"
    )
    return script


# ---- Telegram draft helpers (pure; the control script owns state and replies) ----

FOLLOW_PREFIX = "تكملة"
DONE_TEXT = "تم"
COVER_WORD = "غلاف"


def is_follow_up(text: str) -> bool:
    value = str(text or "").strip()
    return value == FOLLOW_PREFIX or value.startswith(FOLLOW_PREFIX + " ") or value.startswith(FOLLOW_PREFIX + "\n") or value.startswith(FOLLOW_PREFIX + ":")


def follow_up_body(text: str) -> str:
    return str(text).strip()[len(FOLLOW_PREFIX):].lstrip(" :：\n")


def new_draft() -> dict[str, Any]:
    return {"parts": [], "images": []}


def add_part(draft: dict[str, Any], message_id: int, text: str) -> None:
    parts = [p for p in draft["parts"] if p["message_id"] != message_id]
    parts.append({"message_id": int(message_id), "text": str(text)})
    draft["parts"] = sorted(parts, key=lambda p: p["message_id"])


def assemble(draft: Mapping[str, Any]) -> str:
    return "\n".join(p["text"] for p in sorted(draft["parts"], key=lambda p: p["message_id"]))


def image_from_message(message: Mapping[str, Any]) -> dict[str, Any] | None:
    caption = " ".join(str(message.get("caption") or "").split())
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        best = max((p for p in photo if isinstance(p, Mapping)), key=lambda p: int(p.get("file_size") or 0) or int(p.get("width") or 0) * int(p.get("height") or 0), default=None)
        if best and best.get("file_id"):
            return {"message_id": int(message.get("message_id") or 0), "file_id": str(best["file_id"]),
                    "unique_id": str(best.get("file_unique_id") or best["file_id"]), "caption": caption, "kind": "photo"}
    document = message.get("document")
    if isinstance(document, Mapping) and str(document.get("mime_type") or "").startswith("image/") and document.get("file_id"):
        return {"message_id": int(message.get("message_id") or 0), "file_id": str(document["file_id"]),
                "unique_id": str(document.get("file_unique_id") or document["file_id"]), "caption": caption, "kind": "document"}
    return None


def add_image(draft: dict[str, Any], image: Mapping[str, Any]) -> int:
    images = [i for i in draft["images"] if i["unique_id"] != image["unique_id"]]
    images.append(dict(image))
    draft["images"] = sorted(images, key=lambda i: i["message_id"])
    return len(draft["images"])


def classify_art(images: list[Mapping[str, Any]], *, film: bool = False) -> dict[str, Any]:
    """Caption «غلاف» marks the cover.

    Podcast, unmarked: 1 image = episode + cover; 2 = episode then cover; 3+ = all episode.
    Film, unmarked: every image is a scene and the cover comes from the video.
    """
    ordered = sorted(images, key=lambda i: i["message_id"])
    marked = [i for i in ordered if COVER_WORD in str(i.get("caption") or "")]
    rest = [i for i in ordered if i not in marked]
    cover = marked[-1] if marked else None
    if cover is None:
        if film:
            return {"body": rest, "cover": None}
        if len(ordered) == 1:
            return {"body": [ordered[0]], "cover": ordered[0]}
        if len(ordered) == 2:
            return {"body": [ordered[0]], "cover": ordered[1]}
        return {"body": rest, "cover": None}
    return {"body": rest or [cover], "cover": cover}
