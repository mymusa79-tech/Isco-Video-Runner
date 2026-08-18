from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import wraps

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged


_SENTENCE_SPLIT = re.compile(r"(?<=[.!؟!])\s+")
_NEAR_ANCHOR_THRESHOLD = 0.84


def _normalize_arabic(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^\w\s\u0600-\u06FF]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(str(text).strip()) if part.strip()]


def _near_anchor(sentence: str, anchor_sentence: str) -> bool:
    left = _normalize_arabic(sentence)
    right = _normalize_arabic(anchor_sentence)
    if not left or not right:
        return False
    if left == right:
        return True
    return SequenceMatcher(None, left, right).ratio() >= _NEAR_ANCHOR_THRESHOLD


def _strip_anchor_like_sentences(text: str, anchor: str) -> str:
    anchor_sentences = _sentences(anchor)
    if not anchor_sentences:
        return str(text).strip()
    kept: list[str] = []
    for sentence in _sentences(text):
        if any(_near_anchor(sentence, anchor_sentence) for anchor_sentence in anchor_sentences):
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


def _enforce_brand_anchors_once(plan):
    if getattr(plan, "format", "") == "moment" or not getattr(plan, "sections", None):
        return plan

    opener = str(getattr(plan, "identity_opener", "") or "").strip()
    closer = str(getattr(plan, "identity_closer", "") or "").strip()

    first = plan.sections[0]
    last = plan.sections[-1]

    if opener:
        cleaned_first = _strip_anchor_like_sentences(first.narration, opener)
        first.narration = staged._insert_after_first_sentence(cleaned_first, opener)

    if closer:
        cleaned_last = _strip_anchor_like_sentences(last.narration, closer)
        last.narration = f"{cleaned_last.rstrip()} {closer}".strip()

    return plan


def install_brand_anchor_guard() -> None:
    """Sanitize only channel identity anchors after each completed plan build.

    Script Doctor may lightly rewrite an already-present opener/closer and the
    Engine then restores the exact anchor. That can create near-duplicate loops.
    This wrapper removes only sentences strongly similar to the declared identity
    anchor and then restores the exact anchor once. It does not alter middle
    sections, provider calls, audits, or duration gates.
    """
    current = orchestrator.build_plan
    if getattr(current, "_isco_brand_anchor_guard", False):
        print("Brand anchor guard already installed")
        return

    trace_depth = 0

    @wraps(current)
    def guarded_build_plan(*args, **kwargs):
        nonlocal trace_depth
        is_outermost = trace_depth == 0
        trace_depth += 1
        if is_outermost:
            print("PLANNING_BOUNDARY ENTER brand_anchor_guard")
        try:
            plan = current(*args, **kwargs)
            result = _enforce_brand_anchors_once(plan)
        except Exception as exc:
            if is_outermost:
                detail = str(exc).replace("\n", " ")[:220]
                print(
                    "PLANNING_BOUNDARY ERROR brand_anchor_guard "
                    + f"type={type(exc).__name__} detail={detail}"
                )
            raise
        finally:
            trace_depth -= 1
        if is_outermost:
            print("PLANNING_BOUNDARY EXIT brand_anchor_guard")
        return result

    guarded_build_plan._isco_brand_anchor_guard = True
    orchestrator.build_plan = guarded_build_plan
    staged.build_plan = guarded_build_plan
    print("Brand anchor guard installed: opener/closer normalized to one exact anchor each")
