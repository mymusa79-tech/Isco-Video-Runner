from __future__ import annotations
import re
from typing import Any, Mapping

_STOPWORDS = frozenset({"في","من","على","الى","إلى","عن","مع","هذا","هذه","ذلك","تلك","كيف","لماذا","هل","ما","ماذا","كل","او","أو","و","ثم","بعد","قبل","عند","لدى","the","a","an","of","and","to","for"})
_TOKEN_RE = re.compile(r"[A-Za-z0-9%\u0600-\u06ff]+")

def _normalize_token(value: str) -> str:
    token = value.strip().casefold()
    token = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", token)
    token = token.translate(str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ى":"ي","ؤ":"و","ئ":"ي"}))
    if token.startswith("ال") and len(token) > 4:
        token = token[2:]
    return token

def relevance_tokens(value: object) -> set[str]:
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(str(value or "")):
        token = _normalize_token(raw)
        if not token or token in _STOPWORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        out.add(token)
    return out

def market_sample_relevance(query: str, snippet: Mapping[str, Any]) -> tuple[bool, list[str]]:
    query_tokens = relevance_tokens(query)
    if not query_tokens:
        return False, []
    candidate_tokens = relevance_tokens(f"{snippet.get('title') or ''} {snippet.get('description') or ''}")
    overlap = sorted(query_tokens & candidate_tokens)
    minimum = 1 if len(query_tokens) == 1 else 2
    return len(overlap) >= minimum, overlap
