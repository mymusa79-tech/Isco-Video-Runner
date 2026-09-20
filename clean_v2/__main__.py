from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .legacy_cinematic import (
    m8_normalize_media,
    security_media_preflight,
)
from .media import GeminiPrimaryPiperFallbackSynthesizer, StockVisualSource
from .pipeline import CleanV2Pipeline
from .providers import ProviderRouter
from .security_query_adapter import normalize_clean_v2_stock_query


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"invalid boolean environment value: {name}")


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
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Optional fail-closed Clean V2 pre-QC checkpoint directory.",
    )
    parser.add_argument("--max-visuals", type=int, choices=range(1, 6), default=5)
    return parser


def main() -> None:
    args = _parser().parse_args()
    # Azure's key is needed only by the in-process HTTP adapter. Remove it from the
    # inherited environment before any later ffmpeg/tesseract subprocess can start.
    azure_speech_key = str(os.environ.pop("AZURE_SPEECH_KEY", "") or "").strip()
    pipeline = CleanV2Pipeline(
        router=ProviderRouter(),
        voice_synthesizer=GeminiPrimaryPiperFallbackSynthesizer(
            os.environ.get("GEMINI_API_KEY", ""),
            args.piper_model,
            args.voice_manifest,
            tts_model=os.environ.get("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview"),
            azure_api_key=azure_speech_key,
            azure_region=os.environ.get("AZURE_SPEECH_REGION", ""),
            azure_free_tier_confirmed=_env_flag(
                "CLEAN_V2_AZURE_TTS_F0_CONFIRMED"
            ),
            azure_voice_approved=_env_flag(
                "CLEAN_V2_AZURE_TTS_VOICE_APPROVED"
            ),
            allow_piper_fallback=_env_flag("CLEAN_V2_ALLOW_PIPER_FALLBACK"),
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
        resume_from=args.resume_from,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
