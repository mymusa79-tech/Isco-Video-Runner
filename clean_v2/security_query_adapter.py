from __future__ import annotations

"""Narrow Clean V2 compatibility shim for Security V1 stock queries.

Security V1's injection/firewall checks stay unchanged. Clean V2 planning historically
emits comma-separated English stock-search phrases, Run #30 emitted one U+2011
non-breaking hyphen, Run #142 emitted quoted stock-search terms such as 'if-then',
and Run #216 emitted explanatory parentheses/periods and square-bracket placeholders.
Those presentation separators are normalized only after the full original value passes
Security V1's cross-provider injection checks. The resulting value
then crosses Security V1's runtime stock-query gate, whose provider ceiling is coordinated
with Clean V2's 200-character alternate-query contract.
"""

from .legacy_cinematic import CleanV2LayerBlock, _block, security_query_normalizer


def _validate_original_query(value: str) -> str:
    """Run the existing full-value Security V1 text firewall before compatibility work."""
    from isco_video_agent.model_output_schemas import validate_cross_provider_text

    return validate_cross_provider_text(value).as_downstream_data()


def _normalize_observed_separators(value: str) -> str:
    """Normalize only punctuation forms proven by the failed five-run cohort."""
    compatible = (
        value.replace(",", " ")
        .replace("\u2011", "-")
        .replace("'", "")
        .replace('"', "")
        .replace("(", " ")
        .replace(")", " ")
        .replace(".", " ")
        .replace("[", " ")
        .replace("]", " ")
    )
    return " ".join(compatible.split())


def normalize_clean_v2_stock_query(value: str) -> str:
    """Bridge only the punctuation forms observed in the failed five-run cohort.

    Safety order is intentional:
    1. Validate the complete original model output for prompt/URL/role/shell/markup risks.
    2. Convert only presentation punctuation observed in production before stock search:
       commas/parentheses/periods/square brackets to spaces, U+2011 to ASCII hyphen, and
       remove single/double quote marks.
    3. Reuse the Security V1 stock-query gate (same safety checks, 200-char runtime ceiling).

    No other punctuation, non-English text, or malformed query class is repaired here.
    """

    try:
        original = _validate_original_query(value)
        compatible = _normalize_observed_separators(original)
        return security_query_normalizer(compatible)
    except CleanV2LayerBlock:
        raise
    except Exception as exc:
        raise _block("security_v1.query", exc) from exc
