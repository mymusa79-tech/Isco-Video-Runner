from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .legacy_cinematic import (
    m8_normalize_media,
    security_media_preflight,
)
from .media import GeminiOnlyVoiceSynthesizer, StockVisualSource
from .pipeline import CleanV2Pipeline
from .providers import ProviderRouter
from .security_query_adapter import normalize_clean_v2_stock_query


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the minimal Clean V2 video pipeline."
    )
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument(
        "--approved-sha",
        default=os.environ.get("ISCO_APPROVED_BRIEF_SHA256", ""),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--engine-sha", default=os.environ.get("ISCO_ENGINE_SHA", "")
    )
    parser.add_argument(
        "--runner-sha", default=os.environ.get("GITHUB_SHA", "")
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Optional fail-closed Clean V2 pre-QC checkpoint directory.",
    )
    parser.add_argument(
        "--narrative-history",
        type=Path,
        default=(
            Path(os.environ["CLEAN_V2_NARRATIVE_HISTORY_PATH"])
            if os.environ.get("CLEAN_V2_NARRATIVE_HISTORY_PATH")
            else None
        ),
        help=(
            "Optional path to a small JSON file recording recently selected "
            "film narrative formats and Short templates, to avoid repeating the same "
            "shape across recent runs."
        ),
    )
    parser.add_argument("--max-visuals", type=int, choices=range(1, 6), default=5)
    return parser


def main() -> None:
    args = _parser().parse_args()
    from clean_v2.simple_mode import (
        is_simple_mode,
        simple_final_master_qc,
        simple_opening_director,
        simple_visual_qa,
    )

    simple_gates = (
        {
            "visual_qa": simple_visual_qa,
            "opening_director": simple_opening_director,
            "final_master_qc": simple_final_master_qc,
        }
        if is_simple_mode()
        else {}
    )
    visual_source = StockVisualSource(
        query_normalizer=normalize_clean_v2_stock_query,
        media_preflight=security_media_preflight,
        media_transform=m8_normalize_media,
    )
    from clean_v2 import owner_art

    owner_art_dir = owner_art.art_dir()
    if owner_art_dir is not None:
        # Owner Podcast with his own designed images; other formats pass through.
        visual_source = owner_art.OwnerArtVisualSource(visual_source, owner_art_dir)
    pipeline = CleanV2Pipeline(
        **simple_gates,
        router=ProviderRouter(),
        voice_synthesizer=GeminiOnlyVoiceSynthesizer(
            os.environ.get("GEMINI_API_KEY", ""),
            tts_model=os.environ.get("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts"),
        ),
        visual_source=visual_source,
    )
    result = pipeline.run(
        brief_path=args.brief,
        approved_sha256=args.approved_sha,
        output_dir=args.output,
        engine_sha=args.engine_sha,
        runner_sha=args.runner_sha or None,
        max_visuals=args.max_visuals,
        resume_from=args.resume_from,
        narrative_history_path=args.narrative_history,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
