from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Iterator

import isco_video_agent.ai_budget as ai_budget
import isco_video_agent.production_pipeline as production_pipeline
from isco_video_agent.ai_budget import BudgetLedger, Priority


# Run #123 exposed that the original 42/30 hard caps pre-dated the now-live Gold
# packaging/final-critic path and also pre-dated bounded multi-section Vision recovery.
# Keep the budget as an anomaly breaker, but size it to the CURRENT bounded successful
# production graph: one successful wire attempt for each reachable logical AI task,
# including every quality/repair shard that may legitimately be needed. Technical
# provider retries/fallbacks still consume the same cap; they do not inflate the cap
# toward the combinatorial "every provider fails before every success" envelope.
# Optional P2 work is therefore the first thing shed when technical failures consume
# headroom, while enforcing Final Critic is promoted to P0 inside Gold.

_FINAL_CRITIC_PROVIDER_ATTEMPTS = 3  # opening Vision 1 + release text Gemini->OpenRouter max 2
_GOLD_THUMBNAIL_PROVIDER_ATTEMPTS = 4  # concepts 1 + three A/B/C visual-board reviews
_DIRECTOR_OBSERVER_PROVIDER_ATTEMPTS = 1
_AUDIT_ROUNDS = 3  # initial + two RepairDossier re-audits
_AUDITS_PER_ROUND = 3  # factuality + semantic repetition + tone
_MAX_VISION_REVIEWS_PER_SECTION = 4  # two primary + two one-alt-query candidates
_MAX_VISUAL_ALT_QUERY_CALLS_PER_SECTION = 1
_TTS_RUN_EXTRA_PROVIDER_ATTEMPTS = 1
_MAX_REPAIR_DOSSIER_ROUNDS = 2
_MAX_APPEND_REPAIR_CALLS_FILM = 3  # first + bounded completion + narrow missing-target rescue

_SECTION_COUNTS = {"film": 8, "story": 5}


@dataclass(frozen=True)
class SuccessfulAttemptEnvelope:
    format: str
    planning: int
    tts: int
    vision: int
    visual_recovery_text: int
    director_observer: int
    gold_thumbnail_p2: int
    final_critic_p0: int

    @property
    def total(self) -> int:
        return (
            self.planning
            + self.tts
            + self.vision
            + self.visual_recovery_text
            + self.director_observer
            + self.gold_thumbnail_p2
            + self.final_critic_p0
        )

    def to_dict(self) -> dict[str, int | str]:
        return {
            "format": self.format,
            "planning": self.planning,
            "tts": self.tts,
            "vision": self.vision,
            "visual_recovery_text": self.visual_recovery_text,
            "director_observer": self.director_observer,
            "gold_thumbnail_p2": self.gold_thumbnail_p2,
            "final_critic_p0": self.final_critic_p0,
            "total": self.total,
        }


def _successful_envelope(fmt: str) -> SuccessfulAttemptEnvelope:
    sections = _SECTION_COUNTS[fmt]

    # Planning maximum on a SUCCESSFUL bounded path:
    # - outline: 1
    # - writer: up to one capacity-admitted shard per section
    # - Script Doctor: up to one capacity-admitted shard per section
    # - Film append repair: first + bounded completion + narrow rescue = 3
    # - RepairDossier: up to one shard per section, two rounds
    # - quality audits: 3 dimensions x (initial + two re-audits) = 9
    initial_build = 1 + sections + sections + (
        _MAX_APPEND_REPAIR_CALLS_FILM if fmt == "film" else 0
    )
    dossier_repairs = sections * _MAX_REPAIR_DOSSIER_ROUNDS
    quality_audits = _AUDIT_ROUNDS * _AUDITS_PER_ROUND
    planning = initial_build + dossier_repairs + quality_audits

    # Gemini TTS gets one first attempt per section while its circuit remains closed,
    # plus exactly one run-wide bonus cloud attempt. Piper is local and is not a
    # provider attempt.
    tts = sections + _TTS_RUN_EXTRA_PROVIDER_ATTEMPTS

    vision = sections * _MAX_VISION_REVIEWS_PER_SECTION
    visual_recovery_text = sections * _MAX_VISUAL_ALT_QUERY_CALLS_PER_SECTION

    return SuccessfulAttemptEnvelope(
        format=fmt,
        planning=planning,
        tts=tts,
        vision=vision,
        visual_recovery_text=visual_recovery_text,
        director_observer=_DIRECTOR_OBSERVER_PROVIDER_ATTEMPTS,
        gold_thumbnail_p2=_GOLD_THUMBNAIL_PROVIDER_ATTEMPTS,
        final_critic_p0=_FINAL_CRITIC_PROVIDER_ATTEMPTS,
    )


SUCCESSFUL_ATTEMPT_ENVELOPES = {
    fmt: _successful_envelope(fmt) for fmt in _SECTION_COUNTS
}
RUN123_PROVIDER_ATTEMPT_HARD_CAP = {
    fmt: envelope.total for fmt, envelope in SUCCESSFUL_ATTEMPT_ENVELOPES.items()
}

# Final Critic is the only enforcing cloud stage after Gold thumbnail packaging. Once
# release_mode=enforce begins it is P0, not enhancement. Reserve exactly its maximum
# three provider attempts above the generic P2 ceiling; no arbitrary buffer remains.
RUN123_P1_AND_P0_RESERVED_BUFFER = {
    fmt: _FINAL_CRITIC_PROVIDER_ATTEMPTS for fmt in _SECTION_COUNTS
}

# Fail loudly during import if any future edit changes the documented arithmetic
# without updating the expected production envelope.
assert SUCCESSFUL_ATTEMPT_ENVELOPES["film"].to_dict() == {
    "format": "film",
    "planning": 45,
    "tts": 9,
    "vision": 32,
    "visual_recovery_text": 8,
    "director_observer": 1,
    "gold_thumbnail_p2": 4,
    "final_critic_p0": 3,
    "total": 102,
}
assert SUCCESSFUL_ATTEMPT_ENVELOPES["story"].to_dict() == {
    "format": "story",
    "planning": 30,
    "tts": 6,
    "vision": 20,
    "visual_recovery_text": 5,
    "director_observer": 1,
    "gold_thumbnail_p2": 4,
    "final_critic_p0": 3,
    "total": 69,
}


def install_run123_budget_closure() -> None:
    """Install the recalculated production envelope before BudgetLedger construction."""
    if getattr(ai_budget, "_ISCO_RUN123_BUDGET_CLOSURE_INSTALLED", False):
        return

    # These are mutable module-level dictionaries and BudgetLedger reads them at
    # authorize()/summary time. Updating in place also keeps existing imported aliases
    # coherent; no Engine source fork or divergent ledger implementation is created.
    ai_budget.PROVIDER_ATTEMPT_HARD_CAP.update(RUN123_PROVIDER_ATTEMPT_HARD_CAP)
    ai_budget.P1_AND_P0_RESERVED_BUFFER.update(RUN123_P1_AND_P0_RESERVED_BUFFER)
    ai_budget._ISCO_RUN123_BUDGET_CLOSURE_INSTALLED = True
    print(
        "Run123 AI budget closure installed: "
        "film=102 story=69 p2_release_reserve=3 "
        "final_critic=enforce_only_P0 optional_p2=shed_first"
    )


def priority_ceiling(ledger: BudgetLedger, priority: Priority) -> int | None:
    """Return the same run-wide ceiling BudgetLedger.authorize() currently enforces."""
    summary = ledger.to_summary()
    fmt = str(summary.get("format") or "")
    hard_cap = ai_budget.PROVIDER_ATTEMPT_HARD_CAP.get(fmt)
    if hard_cap is None:
        return None
    if priority is Priority.P2:
        return hard_cap - ai_budget.P1_AND_P0_RESERVED_BUFFER.get(fmt, 0)
    if priority is Priority.P1:
        return hard_cap - ai_budget.P0_RESERVED_BUFFER.get(fmt, 0)
    return hard_cap


def remaining_priority_capacity(ledger: BudgetLedger, priority: Priority) -> int | None:
    """Provider-attempt slots still reachable for a priority without issuing a call."""
    ceiling = priority_ceiling(ledger, priority)
    if ceiling is None:
        return None
    used = int(ledger.to_summary()["provider_attempts"]["total"])
    return max(0, ceiling - used)


@contextmanager
def enforcing_final_critic_as_p0() -> Iterator[None]:
    """Promote ONLY the enforced Gold Final Critic to P0 for its synchronous scope.

    Observe-only/shadow critics retain their historical P2 semantics. The Runner calls
    this context only around release_mode="enforce", where a missing critic cannot be
    treated as an optional enhancement: it is the authoritative final release gate.
    """
    original = production_pipeline._final_critic_spec

    def p0_spec(task_id: str, capability):
        return replace(original(task_id, capability), priority=Priority.P0)

    production_pipeline._final_critic_spec = p0_spec
    try:
        yield
    finally:
        production_pipeline._final_critic_spec = original
