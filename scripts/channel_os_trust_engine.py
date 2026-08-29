from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from scripts.channel_os_memory import AutonomyMode, OperationalPolicy
from scripts.retry_after_policy import RetryDelayDecision, retry_delay_decision


class FailureClass(str, Enum):
    TRANSIENT = "Transient"
    RECOVERABLE = "Recoverable"
    NEEDS_CHOICE = "Needs Choice"
    UNSAFE = "Unsafe"


@dataclass(frozen=True)
class CheckpointEvidence:
    resume_allowed: bool
    persist_allowed: bool
    source: str
    reason: str
    binding_digest: str
    binding_digest_verified: bool

    @property
    def verified(self) -> bool:
        return bool(
            self.resume_allowed
            and self.persist_allowed
            and self.binding_digest_verified
            and re.fullmatch(r"[0-9a-f]{64}", str(self.binding_digest or ""))
            and str(self.source or "").strip()
        )


@dataclass(frozen=True)
class FailureEvidence:
    failure_id: str
    transient: bool = False
    provider_retry_after: object = None
    calculated_backoff_seconds: float = 0.0
    wait_budget_seconds: float = 0.0
    checkpoint: CheckpointEvidence | None = None
    editorial_choice_required: bool = False
    unsafe_reason: str = ""
    technical_reason: str = ""


@dataclass(frozen=True)
class TrustDecision:
    failure_id: str
    failure_class: str
    action: str
    automatic_allowed: bool
    reason: str
    existing_retry_owner_certified: bool
    retry_delay: RetryDelayDecision | None
    requires_publish_approval: bool = True
    changes_production_authority: bool = False


def _default_retry_owner_certifier() -> dict[str, object]:
    # Lazy import keeps Channel OS additive. The existing provider reliability module
    # remains the sole authority and is executed exactly as its current contract defines.
    from scripts.provider_retry_ownership import certify_provider_retry_ownership
    return certify_provider_retry_ownership()


class TrustEngine:
    def __init__(self, retry_owner_certifier: Callable[[], dict[str, object]] | None = None) -> None:
        self.retry_owner_certifier = retry_owner_certifier or _default_retry_owner_certifier

    @staticmethod
    def classify(evidence: FailureEvidence) -> FailureClass:
        if str(evidence.unsafe_reason or "").strip():
            return FailureClass.UNSAFE
        if evidence.editorial_choice_required:
            return FailureClass.NEEDS_CHOICE
        if evidence.checkpoint is not None and evidence.checkpoint.verified:
            return FailureClass.RECOVERABLE
        if evidence.transient:
            return FailureClass.TRANSIENT
        if evidence.checkpoint is not None and not evidence.checkpoint.verified:
            return FailureClass.UNSAFE
        return FailureClass.NEEDS_CHOICE

    def decide(self, evidence: FailureEvidence, policy: OperationalPolicy) -> TrustDecision:
        mode = AutonomyMode(policy.autonomy_mode)
        failure_class = self.classify(evidence)
        if policy.require_publish_approval is not True:
            raise RuntimeError("Channel OS publish approval firewall was weakened")

        if failure_class is FailureClass.UNSAFE:
            reason = evidence.unsafe_reason or "checkpoint/recovery evidence is not verified"
            return TrustDecision(evidence.failure_id, failure_class.value, "stop", False, reason, False, None)

        if failure_class is FailureClass.NEEDS_CHOICE:
            return TrustDecision(
                evidence.failure_id, failure_class.value, "ask_user", False,
                evidence.technical_reason or "a user decision is required", False, None,
            )

        if failure_class is FailureClass.RECOVERABLE:
            assert evidence.checkpoint is not None and evidence.checkpoint.verified
            if mode is AutonomyMode.CONSERVATIVE:
                return TrustDecision(
                    evidence.failure_id, failure_class.value, "ask_user_before_verified_resume", False,
                    "conservative policy requires confirmation before checkpoint resume", False, None,
                )
            return TrustDecision(
                evidence.failure_id, failure_class.value, "resume_via_existing_checkpoint_contract", True,
                "checkpoint identity/binding is verified; Channel OS does not create a second checkpoint mechanism",
                False, None,
            )

        # Transient: certify the current provider retry ownership and ask its existing
        # Retry-After policy what the owner may do. Channel OS never executes a retry loop.
        try:
            certification = self.retry_owner_certifier()
            owner_ok = isinstance(certification, dict) and certification.get("status") == "pass"
        except Exception as exc:
            return TrustDecision(
                evidence.failure_id, FailureClass.UNSAFE.value, "stop", False,
                f"existing provider retry ownership could not be certified: {exc}", False, None,
            )
        if not owner_ok:
            return TrustDecision(
                evidence.failure_id, FailureClass.UNSAFE.value, "stop", False,
                "existing provider retry ownership certification did not pass", False, None,
            )
        retry = retry_delay_decision(
            provider_hint=evidence.provider_retry_after,
            calculated_delay_seconds=evidence.calculated_backoff_seconds,
            wait_budget_seconds=evidence.wait_budget_seconds,
        )
        automatic = bool(policy.auto_retry_enabled and mode is not AutonomyMode.CONSERVATIVE)
        if retry.action == "retry":
            action = "defer_to_existing_retry_owner" if automatic else "ask_user_or_existing_owner"
        else:
            action = "defer_to_existing_failover_owner" if automatic else "ask_user_or_existing_owner"
        return TrustDecision(
            evidence.failure_id, failure_class.value, action, automatic,
            retry.reason, True, retry,
        )

    @staticmethod
    def publish_allowed_without_current_gate(policy: OperationalPolicy) -> bool:
        # Deliberately impossible in V1, including Autopilot.
        AutonomyMode(policy.autonomy_mode)
        return False
