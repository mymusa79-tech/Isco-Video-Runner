from __future__ import annotations
import re
from typing import Any, Mapping

_STOPWORDS = frozenset({"في","من","على","الى","إلى","عن","مع","هذا","هذه","ذلك","تلك","كيف","لماذا","هل","ما","ماذا","كل","او","أو","و","ثم","بعد","قبل","عند","لدى","لم","فيه","قد","ان","أن","الي","كان","كانت","the","a","an","of","and","to","for"})
_TOKEN_RE = re.compile(r"[A-Za-z0-9%\u0621-\u063a\u0641-\u064a\u0660-\u0669\u0671-\u06d3]+")

def _normalize_token(value: str) -> str:
    token = value.strip().casefold()
    token = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", token)
    token = token.translate(str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ى":"ي","ؤ":"و","ئ":"ي"}))
    if len(token) > 4:
        # Definite nouns keep the same anchor with common Arabic clitics:
        # المقارنة / والمقارنة / بالمقارنة / للمقارنة.
        token = re.sub(r"^(?:[وف])?(?:[بك]?ال|لل)", "", token)
    return token

def relevance_tokens(value: object) -> set[str]:
    out: set[str] = set()
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", str(value or ""))
    for raw in _TOKEN_RE.findall(text):
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
    # The snippet description is frequently a generic set of motivational
    # hashtags/SEO phrases. It cannot alone prove that the VIDEO covers the
    # search's specific issue. Require a meaningful title anchor and enough
    # distinct query concepts across the title and description.
    title_tokens = relevance_tokens(snippet.get("title"))
    description_tokens = relevance_tokens(snippet.get("description"))
    overlap = sorted(query_tokens & (title_tokens | description_tokens))
    minimum = 1 if len(query_tokens) == 1 else 2
    return bool(query_tokens & title_tokens) and len(overlap) >= minimum, overlap

