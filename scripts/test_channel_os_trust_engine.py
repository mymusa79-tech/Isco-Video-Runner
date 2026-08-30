import unittest

from scripts.channel_os_memory import AutonomyMode, OperationalPolicy
from scripts.channel_os_trust_engine import CheckpointEvidence, FailureClass, FailureEvidence, TrustEngine


def policy(mode="balanced", retry=True):
    return OperationalPolicy(autonomy_mode=mode, auto_retry_enabled=retry, require_publish_approval=True, updated_at="now")


def checkpoint(*, verified=True, resume=True):
    return CheckpointEvidence(
        resume_allowed=resume, persist_allowed=True, source="planning-checkpoint-contract",
        reason="restored", binding_digest="a"*64, binding_digest_verified=verified,
    )


class TrustEngineTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        def certifier():
            self.calls += 1
            return {"status":"pass", "tts_outer_retry_owner":"engine_tts_budget_circuit"}
        self.engine = TrustEngine(certifier)

    def test_transient_defers_to_existing_retry_owner_not_new_loop(self):
        evidence = FailureEvidence("f1", transient=True, provider_retry_after=4, calculated_backoff_seconds=2, wait_budget_seconds=10)
        decision = self.engine.decide(evidence, policy())
        self.assertEqual(decision.failure_class, FailureClass.TRANSIENT.value)
        self.assertEqual(decision.action, "defer_to_existing_retry_owner")
        self.assertTrue(decision.existing_retry_owner_certified)
        self.assertEqual(self.calls, 1)
        self.assertEqual(decision.retry_delay.delay_seconds, 4)

    def test_retry_after_larger_than_budget_defers_to_existing_failover(self):
        evidence = FailureEvidence("f1", transient=True, provider_retry_after=30, calculated_backoff_seconds=1, wait_budget_seconds=5)
        decision = self.engine.decide(evidence, policy())
        self.assertEqual(decision.retry_delay.action, "failover")
        self.assertEqual(decision.action, "defer_to_existing_failover_owner")
        self.assertIsNone(decision.retry_delay.delay_seconds)

    def test_provider_ownership_failure_becomes_unsafe_stop(self):
        engine = TrustEngine(lambda: (_ for _ in ()).throw(RuntimeError("ownership drift")))
        decision = engine.decide(FailureEvidence("f", transient=True, wait_budget_seconds=1), policy())
        self.assertEqual(decision.failure_class, FailureClass.UNSAFE.value)
        self.assertEqual(decision.action, "stop")

    def test_recoverable_requires_verified_checkpoint_binding(self):
        good = self.engine.decide(FailureEvidence("f", checkpoint=checkpoint()), policy())
        self.assertEqual(good.failure_class, FailureClass.RECOVERABLE.value)
        self.assertEqual(good.action, "resume_via_existing_checkpoint_contract")
        bad = self.engine.decide(FailureEvidence("f2", checkpoint=checkpoint(verified=False)), policy())
        self.assertEqual(bad.failure_class, FailureClass.UNSAFE.value)
        self.assertEqual(bad.action, "stop")

    def test_needs_choice_never_auto_executes(self):
        decision = self.engine.decide(FailureEvidence("f", editorial_choice_required=True), policy("autopilot"))
        self.assertEqual(decision.failure_class, FailureClass.NEEDS_CHOICE.value)
        self.assertEqual(decision.action, "ask_user")
        self.assertFalse(decision.automatic_allowed)

    def test_explicit_unsafe_always_stops_in_every_mode(self):
        for mode in AutonomyMode:
            decision = self.engine.decide(FailureEvidence("f", unsafe_reason="security boundary uncertain"), policy(mode.value))
            self.assertEqual(decision.action, "stop")
            self.assertFalse(decision.automatic_allowed)

    def test_conservative_asks_before_recovery_and_transient(self):
        recovered = self.engine.decide(FailureEvidence("f", checkpoint=checkpoint()), policy("conservative"))
        self.assertEqual(recovered.action, "ask_user_before_verified_resume")
        transient = self.engine.decide(FailureEvidence("t", transient=True, wait_budget_seconds=2), policy("conservative"))
        self.assertEqual(transient.action, "ask_user_or_existing_owner")
        self.assertFalse(transient.automatic_allowed)

    def test_balanced_and_autopilot_may_use_safe_existing_technical_owners(self):
        for mode in ("balanced", "autopilot"):
            d = self.engine.decide(FailureEvidence("f", transient=True, wait_budget_seconds=2), policy(mode))
            self.assertTrue(d.automatic_allowed)
            self.assertEqual(d.action, "defer_to_existing_retry_owner")

    def test_publish_firewall_survives_all_three_autonomy_modes(self):
        for mode in AutonomyMode:
            p = policy(mode.value)
            d = self.engine.decide(FailureEvidence("f", unsafe_reason="test"), p)
            self.assertTrue(d.requires_publish_approval)
            self.assertFalse(self.engine.publish_allowed_without_current_gate(p))

    def test_weakened_publish_policy_fails_loud(self):
        bad = OperationalPolicy(autonomy_mode="autopilot", auto_retry_enabled=True, require_publish_approval=False, updated_at="now")
        with self.assertRaises(RuntimeError):
            self.engine.decide(FailureEvidence("f", unsafe_reason="x"), bad)


if __name__ == "__main__":
    unittest.main()
