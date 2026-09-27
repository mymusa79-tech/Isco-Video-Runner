from __future__ import annotations

import os

from isco_video_agent.providers import gemini as engine_gemini
from scripts import provider_preflight


CANONICAL_CONTENT_MODEL = "gemini-3.7-flash"
CANONICAL_TTS_MODEL = "gemini-3.8-flash-tts"
_AB_ALLOWED_CONTENT_MODELS = frozenset({"gemini-3.7-flash", "gemini-3.8-flash"})


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Production model contract requires explicit {name}")
    return value


def _install_ab_thinking_level(level: str) -> None:
    """Experiment-only Gemini thinking control without changing production defaults."""
    normalized = str(level or "").strip().lower()
    if not normalized:
        return
    if normalized not in {"low", "medium", "high"}:
        raise RuntimeError(f"Unsupported Gemini A/B thinking level: {normalized!r}")
    current = engine_gemini.text
    if getattr(current, "_isco_ab_thinking_level", None) == normalized:
        return

    def ab_text(api_key: str, prompt: str, model: str = "gemini-2.5-flash", *, max_output_tokens: int | None = None) -> str:
        client = engine_gemini._client(api_key)
        generation_config: dict[str, object] = {"thinking_level": normalized}
        if max_output_tokens is not None:
            generation_config["max_output_tokens"] = max_output_tokens
        response = client.interactions.create(
            model=engine_gemini._content_model(model),
            input=prompt,
            generation_config=generation_config,
        )
        return response.output_text

    setattr(ab_text, "_isco_ab_thinking_level", normalized)
    engine_gemini.text = ab_text


def install_production_model_contract(orchestrator_module) -> dict[str, str]:
    """Install and prove the single V4 production model contract.

    Isco-Video-Runner is the production authority. The pinned Engine remains reusable
    and may retain legacy/dry-run compatibility names, but real V4 production must use
    the canonical network model explicitly. This installer closes the Run #135 gap
    where Workflow + preflight certified Gemini 3.7 while the Engine's older raw
    free-only whitelist rejected that exact value before planning began.

    The contract deliberately invokes the Engine's real policy guard after installing
    the Runner-owned production allow-list, then verifies both network resolvers. A
    future drift in any of these boundaries therefore fails before provider work.
    """
    content_model = _required_env("GEMINI_CONTENT_MODEL")
    tts_model = _required_env("GEMINI_TTS_MODEL")

    ab_experiment = str(os.environ.get("ISCO_GEMINI_AB_EXPERIMENT") or "").strip() == "1"
    if ab_experiment:
        if content_model not in _AB_ALLOWED_CONTENT_MODELS:
            raise RuntimeError(
                "Gemini A/B experiment content model is not approved: "
                f"requested={content_model!r} allowed={sorted(_AB_ALLOWED_CONTENT_MODELS)!r}"
            )
        effective_content_model = content_model
        _install_ab_thinking_level(os.environ.get("ISCO_GEMINI_AB_THINKING_LEVEL", ""))
    else:
        if content_model != CANONICAL_CONTENT_MODEL:
            raise RuntimeError(
                "V4 production content model contract drift: "
                f"requested={content_model!r} canonical={CANONICAL_CONTENT_MODEL!r}"
            )
        effective_content_model = CANONICAL_CONTENT_MODEL
    if tts_model != CANONICAL_TTS_MODEL:
        raise RuntimeError(
            "V4 production TTS model contract drift: "
            f"requested={tts_model!r} canonical={CANONICAL_TTS_MODEL!r}"
        )

    # Runner owns production policy. Replace, do not append to, the Engine's legacy
    # dry-run whitelist so a stale alias can never become the production truth again.
    orchestrator_module.FREE_CONTENT_MODELS = {effective_content_model}
    orchestrator_module.FREE_TTS_MODELS = {CANONICAL_TTS_MODEL}

    # Exercise the exact guard that failed Run #135, not a parallel imitation.
    orchestrator_module._enforce_free_only_models(content_model, tts_model)

    engine_network_model = engine_gemini._content_model(content_model)
    preflight_network_model = provider_preflight._gemini_runtime_content_model(content_model)
    if engine_network_model != effective_content_model:
        raise RuntimeError(
            "Engine Gemini resolver drift: "
            f"requested={content_model!r} resolved={engine_network_model!r}"
        )
    if preflight_network_model != effective_content_model:
        raise RuntimeError(
            "Runner provider-preflight resolver drift: "
            f"requested={content_model!r} resolved={preflight_network_model!r}"
        )
    if engine_network_model != preflight_network_model:
        raise RuntimeError(
            "Gemini production resolver disagreement: "
            f"engine={engine_network_model!r} preflight={preflight_network_model!r}"
        )

    print(
        "Production model contract installed and verified: "
        f"content={content_model} network={engine_network_model} tts={tts_model}"
    )
    return {
        "content_model": content_model,
        "network_content_model": engine_network_model,
        "tts_model": tts_model,
    }
