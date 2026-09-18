from __future__ import annotations

"""Narrow Clean V2 compatibility shim for Security V1 stock queries.

The certified Security V1 schema stays unchanged. Clean V2 planning historically emits
comma-separated English stock-search phrases, and Run #30 also emitted one U+2011
non-breaking hyphen. Those presentation separators are normalized only after the full
original value passes Security V1's cross-provider injection checks. The resulting value
still has to pass the unchanged Security V1 visual-query validator.
"""

from .legacy_cinematic import CleanV2LayerBlock, _block, security_query_normalizer


def normalize_clean_v2_stock_query(value: str) -> str:
    """Bridge only the punctuation forms observed in the failed five-run cohort.

    Safety order is intentional:
    1. Validate the complete original model output for prompt/URL/role/shell/markup risks.
    2. Convert commas to spaces and U+2011 to the ASCII hyphen accepted by stock search.
    3. Reuse the unchanged Security V1 stock-query normalizer/validator.

    No other punctuation, non-English text, or malformed query class is repaired here.
    """

    try:
        from isco_video_agent.model_output_schemas import validate_cross_provider_text

        original = validate_cross_provider_text(value).as_downstream_data()
        compatible = original.replace(",", " ").replace("\u2011", "-")
        compatible = " ".join(compatible.split())
        return security_query_normalizer(compatible)
    except CleanV2LayerBlock:
        raise
    except Exception as exc:
        raise _block("security_v1.query", exc) from exc
