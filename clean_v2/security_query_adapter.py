from __future__ import annotations

"""Narrow Clean V2 compatibility shim for Security V1 stock queries.

Security V1's injection/firewall checks stay unchanged. Clean V2 planning historically
emits comma-separated English stock-search phrases, Run #30 emitted one U+2011
non-breaking hyphen, Run #142 emitted quoted stock-search terms such as 'if-then',
Run #216 emitted explanatory parentheses/periods plus two literal placeholders:
[specific time] and [specific action], and Run #14 (Short, first Cold attempt after
#812's inner_dialogue fix) emitted a Latin letter with a diacritic ('café') inside a
widened (>80 char) query, which Security V1's ASCII-only gate for that length class
rejects outright. Ordinary presentation punctuation is normalized only after the full
original value passes Security V1's cross-provider injection checks. Those two exact
bracket placeholders are the sole exception: their brackets are removed, then the
complete candidate is revalidated through the same firewall before stock search.
The resulting value
then crosses Security V1's runtime stock-query gate, whose provider ceiling is coordinated
with Clean V2's 200-character alternate-query contract.
"""

import unicodedata

from .legacy_cinematic import CleanV2LayerBlock, _block, security_query_normalizer


_RUN216_SAFE_BRACKET_PLACEHOLDERS = (
    "[specific time]",
    "[specific action]",
)


def _validate_original_query(value: str) -> str:
    """Run the existing full-value Security V1 text firewall before compatibility work."""
    from isco_video_agent.model_output_schemas import validate_cross_provider_text

    return validate_cross_provider_text(value).as_downstream_data()


def _validate_original_or_run216_placeholders(value: str) -> str:
    """Keep the firewall first, with one exact production-proven placeholder bridge.

    Security V1 intentionally rejects bracketed/structured text. Run #216 used exactly
    two benign stock-search placeholders that look structured only because of their
    presentation brackets. If and only if the original rejection is that markup class,
    strip brackets from those exact literals, reject any remaining bracket syntax, then
    run the complete value through the same cross-provider firewall again.
    """

    try:
        return _validate_original_query(value)
    except Exception as exc:
        if str(exc) != "model_output_markup_or_structured_instruction_rejected":
            raise
        compatible = value
        replaced = False
        for placeholder in _RUN216_SAFE_BRACKET_PLACEHOLDERS:
            if placeholder in compatible:
                compatible = compatible.replace(
                    placeholder,
                    placeholder[1:-1],
                )
                replaced = True
        if not replaced or "[" in compatible or "]" in compatible:
            raise
        return _validate_original_query(compatible)


def _fold_latin_diacritics(value: str) -> str:
    """Fold diacritics only when they belong to an ASCII Latin base letter.

    NFD performs canonical decomposition without the broader compatibility folding of
    NFKD. A nonspacing mark is dropped only while the current combining sequence is
    attached to an ASCII Latin base (A-Z/a-z). Full-width/compatibility characters and
    combining marks attached to non-Latin scripts remain unchanged for Security V1 to
    accept or reject normally.
    """
    decomposed = unicodedata.normalize("NFD", value)
    folded: list[str] = []
    ascii_latin_base = False
    for ch in decomposed:
        if unicodedata.category(ch) == "Mn":
            if ascii_latin_base:
                continue
            folded.append(ch)
            continue
        folded.append(ch)
        ascii_latin_base = ("A" <= ch <= "Z") or ("a" <= ch <= "z")
    return "".join(folded)


def _normalize_observed_separators(value: str) -> str:
    """Normalize only punctuation/character forms proven by production failures."""
    compatible = (
        value.replace(",", " ")
        .replace("\u2011", "-")
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("'", "")
        .replace('"', "")
        .replace("(", " ")
        .replace(")", " ")
        .replace(".", " ")
    )
    # Fold diacritics last so the existing named-character replacements remain
    # authoritative. NFD is intentionally used instead of compatibility folding.
    compatible = _fold_latin_diacritics(compatible)
    return " ".join(compatible.split())


def normalize_clean_v2_stock_query(value: str) -> str:
    """Bridge only the punctuation forms observed in the failed five-run cohort.

    Safety order is intentional:
    1. Validate the complete original model output for prompt/URL/role/shell/markup risks.
       Only the exact Run #216 literals [specific time] and [specific action] may bridge
       a markup-only rejection; after stripping those brackets, the complete candidate is
       immediately revalidated by the same firewall. Any other bracket syntax stays blocked.
    2. After validation, convert observed presentation punctuation before stock search:
       commas/parentheses/periods to spaces, U+2011 to ASCII hyphen, remove quotes, and
       fold Latin letters with diacritics to their plain ASCII base (café -> cafe).
    3. Reuse the Security V1 stock-query gate (same safety checks, 200-char runtime ceiling).

    No other punctuation, non-English text, or malformed query class is repaired here.
    """

    try:
        original = _validate_original_or_run216_placeholders(value)
        compatible = _normalize_observed_separators(original)
        return security_query_normalizer(compatible)
    except CleanV2LayerBlock:
        raise
    except Exception as exc:
        raise _block("security_v1.query", exc) from exc
