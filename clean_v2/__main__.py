from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .legacy_cinematic import (
    m8_normalize_media,
    security_media_preflight,
)
from .media import PiperVoiceSynthesizer, StockVisualSource
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
        "--piper-model",
        type=Path,
        required=True,
    )
    parser.add_argument("--voice-manifest", type=Path)
    parser.add_argument("--max-visuals", type=int, choices=range(1, 7), default=6)
    return parser


def main() -> None:
    args = _parser().parse_args()
    pipeline = CleanV2Pipeline(
        router=ProviderRouter(),
        voice_synthesizer=PiperVoiceSynthesizer(
            args.piper_model, args.voice_manifest
        ),
        visual_source=StockVisualSource(
            query_normalizer=normalize_clean_v2_stock_query,
            media_preflight=security_media_preflight,
            media_transform=m8_normalize_media,
        ),
    )
    result = pipeline.run(
        brief_path=args.brief,
        approved_sha256=args.approved_sha,
        output_dir=args.output,
        engine_sha=args.engine_sha,
        runner_sha=args.runner_sha or None,
        max_visuals=args.max_visuals,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
