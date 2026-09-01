from __future__ import annotations

import sys
from pathlib import Path

# GitHub Actions executes this wrapper directly as ``python scripts/...``.
# Restore the repository root before importing package-owned modules.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import telegram_topic_research_v2_core as core


def __getattr__(name: str):
    return getattr(core, name)


def _state_arg() -> Path | None:
    args = sys.argv[1:]
    try:
        index = args.index("--state")
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    return Path(args[index + 1])


def _durable_pending_research_exists(state_path: Path | None) -> bool:
    if state_path is None or not state_path.is_file():
        return False
    state = core.panel.load_state(state_path)
    actions = state.get("pending_actions")
    if not isinstance(actions, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("status") or "") == "pending"
        and str(item.get("kind") or "long") in {"long", "short"}
        for item in actions
    )


def _claim_pending_scheduler_retry_without_polling(mode: str) -> bool:
    """Let the 5-minute scheduler retry durable research without getUpdates.

    Edge webhook ingress and fallback Telegram polling are mutually exclusive. A
    provider timeout must not strand a durable research action merely because the
    webhook remains active, so a scheduled ``poll`` pass may claim already-saved
    research work while still refusing to poll Telegram for new updates.
    """
    if mode != "poll":
        return False
    state_path = _state_arg()
    if not _durable_pending_research_exists(state_path):
        return False
    from scripts import telegram_webhook_replay_core as webhook_core

    if not webhook_core.webhook_active():
        return False
    core.panel._github_output("needs_engine", "true")
    core.panel._github_output("needs_production", "false")
    print(
        "Telegram webhook remains active; claiming durable pending topic research "
        "for automatic retry without calling getUpdates"
    )
    return True


def _install_live_topic_provider_reliability(mode: str) -> None:
    if mode != "research":
        return
    import isco_video_agent.research as engine_research
    from scripts.research_provider_reliability import gemini_research_call_with_fallback
    from scripts.topic_research_market_reliability import (
        MAX_MARKET_PROBE_CANDIDATES,
        install_market_probe_reliability,
        install_shortfall_reason,
    )

    # Topic Research V2 owns candidate-generation policy; provider transport
    # reliability stays in the existing Runner adapter. Inject the already-certified
    # bounded Gemini retry -> OpenRouter free failover into Engine select_topic()
    # without changing the Engine's generic fallback semantics or Production paths.
    engine_research.json_text = gemini_research_call_with_fallback

    # Keep the three-live-candidate gate unchanged, but stop treating the first five
    # candidates as the entire evidence universe. The Engine produces up to ten
    # distinct candidates; expose that bounded pool to the adaptive market probe,
    # which checks five first and expands only when the live contract still lacks
    # evidence. This preserves YouTube quota when the first batch is sufficient.
    core.MAX_YOUTUBE_MARKET_PROBES = MAX_MARKET_PROBE_CANDIDATES
    install_market_probe_reliability(engine_research)
    install_shortfall_reason(core)


def main() -> None:
    core.memory_ui._install_policy()
    from scripts import telegram_canonical_status_bridge as canonical_status_bridge
    from scripts import telegram_creator_control_center_v5 as creator_v5
    from scripts import telegram_operator_mission_control as operator_mission_control
    from scripts import telegram_persistent_control_ui as persistent_ui
    from scripts import telegram_rich_integration as rich_integration
    from scripts import telegram_session_continuity as session_continuity

    persistent_ui.install()
    core.memory_ui._install_library_split()
    core.memory_ui._install_choice_clarity()
    canonical_status_bridge.install()
    rich_integration.install()
    core.active._install()
    core.install_v2()
    # Install last so V5 is a presentation/navigation layer over the fully
    # certified Topic Research V2 + Production authority stack.
    creator_v5.install(core)
    # Bind session continuity after every approval/UI wrapper is final. This keeps
    # latest Long and latest Short cards independently actionable without bypassing
    # the existing approval or Production activation gates.
    session_continuity.install(active=core.active, panel=core.panel)
    # Mission Control is the final operator projection: it normalizes state labels
    # and receipts only, while the exact typed-confirmation authority remains intact.
    operator_mission_control.install()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    core.memory_ui._require_poll_identity(mode)
    if _claim_pending_scheduler_retry_without_polling(mode):
        return
    _install_live_topic_provider_reliability(mode)
    core.panel.main()


if __name__ == "__main__":
    main()
