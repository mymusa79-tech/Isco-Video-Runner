import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.channel_brain import BrainAnalysis, ChannelBrain, VideoPerformance
from scripts.channel_os_memory import LearnedPreference
from scripts.channel_os_mission_control import MissionItem, MissionSnapshot
from scripts.channel_os_proactive_operator import (
    InterventionLevel,
    NotificationBudget,
    ProactiveDeliveryLedger,
    ProactiveOperator,
    ProactiveSignal,
    render_telegram,
)
from scripts.channel_os_trust_engine import TrustDecision


NOW = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)


def snapshot(*items):
    counts = {name: 0 for name in ("Ideas", "Ready", "Producing", "Needs Me", "Scheduled", "Published", "Problems")}
    for item in items:
        counts[item.mission_state] += 1
    return MissionSnapshot(tuple(items), counts, NOW.isoformat(), 0)


def item(video_id="v1", state="Needs Me", *, run_id="10", reason="editorial choice", source="github-actions"):
    return MissionItem(video_id, f"Video {video_id}", state, run_id, source, NOW.isoformat(), reason)


def trust(failure_id="f1", failure_class="Needs Choice", action="ask_user", reason="choice required"):
    return TrustDecision(
        failure_id=failure_id,
        failure_class=failure_class,
        action=action,
        automatic_allowed=False,
        reason=reason,
        existing_retry_owner_certified=False,
        retry_delay=None,
        requires_publish_approval=True,
        changes_production_authority=False,
    )


def perf(i, *, views=1000, ctr=.10, retention=.50):
    return VideoPerformance(
        video_id=f"v{i}", content_type="long", topic_family="focus", duration_seconds=480,
        published_at=f"2026-08-{10+i:02d}T18:00:00+00:00", views=views, ctr=ctr, retention=retention,
    )


def strong_brain_analysis():
    brain = ChannelBrain(min_cohort_size=3, recent_n=8)
    history = [perf(i, views=1000, ctr=.10, retention=.50) for i in range(1, 9)]
    return brain.analyze(perf(9, views=700, ctr=.06, retention=.35), history)


def weak_brain_analysis():
    brain = ChannelBrain(min_cohort_size=3, recent_n=5)
    return brain.analyze(perf(9, views=700, ctr=.06, retention=.35), [perf(1), perf(2), perf(3)])


def learned(*, evidence=3, confidence=.85, status="active"):
    refs = tuple(f"e{i}" for i in range(evidence))
    return LearnedPreference(
        preference_id="lp1", hypothesis="prefers concise hooks", scope="hook",
        confidence=confidence, evidence_count=evidence, recency="2026-08-29T00:00:00+00:00",
        evidence_refs=refs, explanation="Repeated choices favored concise hooks.",
        created_at="2026-08-20T00:00:00+00:00", last_updated_at="2026-08-29T00:00:00+00:00",
        status=status,
    )


class ProactiveOperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = ProactiveDeliveryLedger(self.tmp.name)
        self.operator = ProactiveOperator(self.ledger)

    def tearDown(self):
        self.tmp.cleanup()

    def test_needs_me_live_state_becomes_interrupt_now(self):
        signals = self.operator.interrupt_signals(snapshot(item()))
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal.level, InterventionLevel.INTERRUPT_NOW.value)
        self.assertEqual(signal.source, "mission-control-live-state")
        self.assertTrue(any("source=github-actions" in evidence for evidence in signal.evidence))
        self.assertTrue(signal.action_callback.startswith("cmd:channelos-needs:"))
        self.assertEqual(signal.authority, "advisory_only")

    def test_trust_needs_choice_and_unsafe_are_interrupts(self):
        signals = self.operator.interrupt_signals(
            snapshot(),
            [trust("f1", "Needs Choice", "ask_user"), trust("f2", "Unsafe", "stop", "security boundary uncertain")],
        )
        self.assertEqual({signal.level for signal in signals}, {InterventionLevel.INTERRUPT_NOW.value})
        self.assertEqual(len(signals), 2)
        self.assertTrue(all(any("publish_approval=required" == evidence for evidence in signal.evidence) for signal in signals))

    def test_generic_problem_is_digest_not_interrupt_without_trust_escalation(self):
        snap = snapshot(item("v2", "Problems", reason="provider exhausted"))
        self.assertEqual(self.operator.interrupt_signals(snap), ())
        digest = self.operator.digest_signal(snap)
        self.assertIsNotNone(digest)
        self.assertEqual(digest.level, InterventionLevel.DIGEST.value)
        self.assertIn("Problems", digest.reason)

    def test_interrupt_is_deduped_until_material_state_changes(self):
        first = self.operator.interrupt_signals(snapshot(item(reason="choice A")))[0]
        decision = self.operator.delivery_decision(first, now=NOW)
        self.assertTrue(decision.deliver)
        self.ledger.record(first, sent_at=NOW)
        repeated = self.operator.interrupt_signals(snapshot(item(reason="choice A")))[0]
        suppressed = self.operator.delivery_decision(repeated, now=NOW + timedelta(hours=1))
        self.assertFalse(suppressed.deliver)
        self.assertEqual(suppressed.suppression_reason, "duplicate_interrupt_without_material_state_change")
        changed = self.operator.interrupt_signals(snapshot(item(reason="choice B")))[0]
        self.assertTrue(self.operator.delivery_decision(changed, now=NOW + timedelta(hours=1)).deliver)

    def test_low_confidence_or_small_sample_brain_does_not_create_opportunity(self):
        analysis = weak_brain_analysis()
        self.assertIsNone(self.operator.opportunity_from_brain(analysis))

    def test_strong_brain_signal_contains_reason_evidence_action_and_is_advisory(self):
        signal = self.operator.opportunity_from_brain(strong_brain_analysis())
        self.assertIsNotNone(signal)
        self.assertEqual(signal.level, InterventionLevel.OPPORTUNITY.value)
        self.assertGreaterEqual(signal.confidence, .70)
        self.assertTrue(signal.reason)
        self.assertTrue(signal.evidence)
        self.assertTrue(signal.action_label)
        self.assertEqual(signal.authority, "advisory_only")
        self.assertNotIn("publish", signal.action_callback.casefold())

    def test_single_or_low_evidence_learned_preference_is_not_proactive(self):
        self.assertIsNone(self.operator.opportunity_from_learned_preference(learned(evidence=1, confidence=.95)))
        self.assertIsNone(self.operator.opportunity_from_learned_preference(learned(evidence=3, confidence=.50)))

    def test_strong_learned_preference_is_explainable_advisory_opportunity(self):
        signal = self.operator.opportunity_from_learned_preference(learned())
        self.assertIsNotNone(signal)
        self.assertEqual(signal.level, InterventionLevel.OPPORTUNITY.value)
        self.assertTrue(any("evidence_count=3" in evidence for evidence in signal.evidence))
        self.assertTrue(any("confidence=0.85" in evidence for evidence in signal.evidence))
        self.assertEqual(signal.authority, "advisory_only")

    def test_do_not_promote_preference_is_suppressed(self):
        self.assertIsNone(self.operator.opportunity_from_learned_preference(learned(status="do_not_promote")))

    def test_opportunity_budget_is_two_per_rolling_24_hours(self):
        for index in range(2):
            signal = ProactiveSignal(
                signal_id=f"op{index}", level="opportunity", title="Opportunity", reason="Strong evidence",
                evidence=(f"evidence-{index}",), action_label="Review", action_callback=f"cmd:channelos-op:{index}",
                confidence=.9, state_token=str(index),
            )
            self.assertTrue(self.operator.delivery_decision(signal, now=NOW).deliver)
            self.ledger.record(signal, sent_at=NOW + timedelta(minutes=index))
        third = ProactiveSignal(
            signal_id="op3", level="opportunity", title="Opportunity", reason="Strong evidence",
            evidence=("evidence-3",), action_label="Review", action_callback="cmd:channelos-op:3",
            confidence=.95, state_token="3",
        )
        denied = self.operator.delivery_decision(third, now=NOW + timedelta(hours=1))
        self.assertFalse(denied.deliver)
        self.assertEqual(denied.suppression_reason, "opportunity_notification_budget_exhausted")
        self.assertTrue(self.operator.delivery_decision(third, now=NOW + timedelta(hours=25)).deliver)

    def test_digest_budget_is_one_per_rolling_24_hours(self):
        first = ProactiveSignal(
            signal_id="d1", level="digest", title="Digest", reason="Low urgency",
            evidence=("digest evidence",), action_label="Open", action_callback="cmd:channelos-refresh",
            confidence=1.0, state_token="d1",
        )
        self.assertTrue(self.operator.delivery_decision(first, now=NOW).deliver)
        self.ledger.record(first, sent_at=NOW)
        second = ProactiveSignal(
            signal_id="d2", level="digest", title="Digest 2", reason="Low urgency",
            evidence=("digest evidence 2",), action_label="Open", action_callback="cmd:channelos-refresh",
            confidence=1.0, state_token="d2",
        )
        denied = self.operator.delivery_decision(second, now=NOW + timedelta(hours=1))
        self.assertFalse(denied.deliver)
        self.assertEqual(denied.suppression_reason, "digest_notification_budget_exhausted")

    def test_interrupt_is_outside_opportunity_budget_but_still_deduped(self):
        for index in range(2):
            op = ProactiveSignal(
                signal_id=f"op{index}", level="opportunity", title="Opportunity", reason="Reason",
                evidence=("e",), action_label="Review", action_callback=f"cmd:channelos-op:{index}", confidence=.9,
                state_token=str(index),
            )
            self.ledger.record(op, sent_at=NOW)
        interrupt = self.operator.interrupt_signals(snapshot(item()))[0]
        self.assertTrue(self.operator.delivery_decision(interrupt, now=NOW).deliver)

    def test_callback_length_and_publish_upload_actions_are_rejected(self):
        with self.assertRaises(ValueError):
            ProactiveSignal(
                signal_id="long", level="digest", title="x", reason="x", evidence=("e",),
                action_label="x", action_callback="x" * 65, confidence=1.0,
            )
        for callback in ("cmd:publish-now", "cmd:upload-video"):
            with self.assertRaises(ValueError):
                ProactiveSignal(
                    signal_id="bad", level="opportunity", title="x", reason="x", evidence=("e",),
                    action_label="x", action_callback=callback, confidence=.9,
                )

    def test_ledger_stores_delivery_accounting_only(self):
        signal = self.operator.interrupt_signals(snapshot(item()))[0]
        self.ledger.record(signal, sent_at=NOW)
        payload = json.loads(Path(self.ledger.path).read_text(encoding="utf-8"))
        self.assertEqual(set(payload[0]), {"fingerprint", "level", "sent_at"})
        raw = json.dumps(payload)
        self.assertNotIn("video_id", raw)
        self.assertNotIn("reason", raw)
        self.assertNotIn("preference", raw)
        self.assertNotIn("live_state", raw)

    def test_render_is_text_only_explainable_and_has_no_mini_app(self):
        signal = self.operator.opportunity_from_brain(strong_brain_analysis())
        text, keyboard = render_telegram(signal)
        self.assertIn("السبب:", text)
        self.assertIn("الدليل:", text)
        self.assertIn("Confidence:", text)
        self.assertIn("Authority: advisory only", text)
        self.assertTrue(keyboard)
        self.assertNotIn("web_app", str(keyboard))
        self.assertNotIn("MiniApp", str(keyboard))

    def test_operator_returns_plans_only_and_never_executes_side_effects(self):
        source = Path("scripts/channel_os_proactive_operator.py").read_text(encoding="utf-8")
        forbidden = ("urllib.request", "requests.post", "subprocess.run", "workflow_dispatch", "sendMessage", "send_message")
        for token in forbidden:
            self.assertNotIn(token, source)
        signal = self.operator.opportunity_from_brain(strong_brain_analysis())
        self.assertEqual(signal.authority, "advisory_only")


if __name__ == "__main__":
    unittest.main()
