from __future__ import annotations

import re


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _semantic_key(value: object) -> str:
    text = _clean(value).casefold()
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def structural_ai_flags(text: object, *, short_form: bool = False) -> tuple[str, ...]:
    raw = _clean(text)
    if not raw:
        return ("empty_narration",)
    flags: list[str] = []
    sentences = [s.strip() for s in re.split(r"[.!؟!]+", raw) if s.strip()]

    not_x_but_y = len(re.findall(r"(?:ليس|ليست|ليسَ)[^.!؟!]{0,90}(?:بل|وإنما)", raw))
    if not_x_but_y >= (2 if short_form else 3):
        flags.append("repeated_not_x_but_y")

    triads = len(re.findall(r"(?:أولًا|أولا).{0,180}(?:ثانيًا|ثانيا).{0,180}(?:ثالثًا|ثالثا)", raw))
    if triads >= 1 and (short_form or len(sentences) < 14):
        flags.append("formulaic_rhetorical_triad")

    rhetorical_questions = sum(1 for s in re.split(r"(?<=؟)", raw) if "؟" in s)
    if rhetorical_questions >= (3 if short_form else 6):
        flags.append("excessive_rhetorical_questions")

    normalized_sentences = [_semantic_key(s) for s in sentences if len(_semantic_key(s).split()) >= 4]
    if len(normalized_sentences) != len(set(normalized_sentences)):
        flags.append("duplicate_sentence")

    generic_closers = ("كل ما عليك", "غير حياتك", "آمن بنفسك", "لا تستسلم", "ابدأ الآن")
    tail = raw[-240:]
    if any(marker in tail for marker in generic_closers):
        flags.append("generic_motivational_closer")

    return tuple(dict.fromkeys(flags))
