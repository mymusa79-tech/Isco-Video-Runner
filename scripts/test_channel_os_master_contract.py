import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.channel_brain import ChannelBrain, VideoPerformance
from scripts.channel_os_memory import AutonomyMode, ChannelOSMemory, LiveState
from scripts.channel_os_mission_control import MissionControl, VideoEntity
from scripts.channel_os_publication_policy import (
    channel_os_youtube_publish_allowed,
    channel_os_youtube_upload_allowed,
    publication_contract,
)
from scripts.channel_os_trust_engine import CheckpointEvidence, FailureEvidence, TrustEngine
from scripts.editorial_decision_packet import EditorialPacketStore, bind_to_production_request, verify_production_binding


class Provider:
    def __init__(self, states=None, fail=False):
        self.states = states or {}
        self.fail = fail

    def fetch(self, video_id):
        if self.fail:
            raise RuntimeError("live source unavailable")
        return self.states[video_id]


def live(video_id, status, run="1", reason=""):
    return LiveState(video_id, status, "production", run, "github-actions", "2026-08-29T00:00:00+00:00", True, reason)


def perf(i, kind="long", views=1000, ctr=.08, retention=.45):
    return VideoPerformance(
        f"v{i}", kind, "focus", 480 if kind == "long" else 45,
        f"2026-08-{10+i:02d}T18:00:00+00:00", views, ctr, retention,
    )


FIELDS = dict(
    topic="Topic", angle="Angle", promise="Promise", hook="Hook", tone="Warm",
    length="8m", visual_direction="Minimal", short_long_strategy="Long + shorts",
    publish_target="Thu 20:00",
)


class MasterApprovalContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = ChannelOSMemory(self.tmp.name, min_evidence=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_memory_isolation(self):
        self.assertEqual(
            set(self.memory.persistent_paths),
            {"stable_preferences", "episodic_decisions", "operational_policies", "learned_preferences"},
        )
        self.assertFalse(any("live" in path.name for path in self.memory.persistent_paths.values()))

    def test_02_single_sample_generalization_is_blocked(self):
        learned = self.memory.upsert_learned_preference(
            preference_id="lp", hypothesis="prefers X", scope="topic", confidence=.5,
            evidence_refs=["e1"], explanation="one event",
        )
        self.assertEqual(learned.evidence_count, 1)
        with self.assertRaises(ValueError):
            self.memory.promote_learned_preference(preference_id="lp", key="topic", value="X")

    def test_03_learned_preference_explainability(self):
        learned = self.memory.upsert_learned_preference(
            preference_id="lp", hypothesis="prefers X", scope="topic", confidence=.8,
            evidence_refs=["e1", "e2", "e3"], explanation="repeated choices",
        )
        self.assertFalse({"confidence", "evidence_count", "recency", "evidence_refs", "explanation"} - set(learned.explain()))

    def test_04_stale_state_loses_to_live_source(self):
        item = MissionControl(self.memory, Provider({"v": live("v", "failed", "5", "failed live")})).snapshot(
            [VideoEntity("v", "Video", "Ready", run_id="5")]
        ).items[0]
        self.assertEqual(item.mission_state, "Problems")

    def test_05_source_unavailable_never_falls_back_to_memory(self):
        item = MissionControl(self.memory, Provider(fail=True)).snapshot(
            [VideoEntity("v", "Video", "Ready", run_id="5")]
        ).items[0]
        self.assertEqual((item.source, item.mission_state), ("live-source-unavailable", "Problems"))

    def test_06_channel_brain_is_advisory_only(self):
        result = ChannelBrain(min_cohort_size=3).analyze(perf(9, views=500), [perf(i) for i in range(1, 5)])
        self.assertEqual(result.as_context()["authority"], "advisory_only")
        self.assertFalse(result.as_context()["may_auto_override"])

    def test_07_missing_analytics_are_not_fabricated(self):
        target = VideoPerformance("x", "long", "focus", 480, "2026-08-29T18:00:00+00:00", 1000, None, None)
        result = ChannelBrain(min_cohort_size=3).analyze(target, [perf(i) for i in range(1, 5)])
        self.assertEqual(set(result.missing_metrics), {"ctr", "retention"})

    def test_08_long_short_baselines_are_separate(self):
        result = ChannelBrain(min_cohort_size=3).analyze(
            perf(9, "long"),
            [perf(1, "short"), perf(2, "short"), perf(3, "short"), perf(4), perf(5), perf(6)],
        )
        self.assertTrue(all(not any(v in {"v1", "v2", "v3"} for v in cohort.evidence_ids) for cohort in result.comparisons))

    def test_09_packet_version_binding(self):
        store = EditorialPacketStore(self.tmp.name)
        store.create(packet_id="p1", **FIELDS)
        approved = store.approve("p1", expected_version=1)
        request = bind_to_production_request({"request_id": "r"}, approved)
        edited = store.edit("p1", expected_version=1, changes={"hook": "H2"})
        self.assertTrue(verify_production_binding(request, approved))
        self.assertFalse(verify_production_binding(request, edited))
        self.assertFalse(edited.approved_by_user)

    def test_10_alternatives_are_non_mutating(self):
        store = EditorialPacketStore(self.tmp.name)
        store.create(packet_id="p2", **FIELDS)
        store.alternatives("p2", expected_version=1, field="hook", values=["A", "B", "C"])
        self.assertEqual((store.current("p2").version, store.current("p2").hook), (1, "Hook"))

    def test_11_retry_ownership_is_existing_owner_only(self):
        calls = []
        engine = TrustEngine(lambda: (calls.append(1) or {"status": "pass"}))
        decision = engine.decide(
            FailureEvidence("f", transient=True, provider_retry_after=2, wait_budget_seconds=5),
            self.memory.get_policy(),
        )
        self.assertEqual(calls, [1])
        self.assertIn("existing_retry_owner", decision.action)
        self.assertTrue(decision.existing_retry_owner_certified)

    def test_12_checkpoint_requires_verified_binding(self):
        decision = TrustEngine(lambda: {"status": "pass"}).decide(
            FailureEvidence("f", checkpoint=CheckpointEvidence(True, True, "planning-checkpoint", "ok", "a" * 64, False)),
            self.memory.get_policy(),
        )
        self.assertEqual((decision.failure_class, decision.action), ("Unsafe", "stop"))

    def test_13_unsafe_always_stops(self):
        engine = TrustEngine(lambda: {"status": "pass"})
        for mode in AutonomyMode:
            policy = self.memory.set_policy(
                autonomy_mode=mode, auto_retry_enabled=True, explicit_user_change=True
            )
            self.assertEqual(engine.decide(FailureEvidence("f", unsafe_reason="unsafe"), policy).action, "stop")

    def test_14_publish_firewall_all_modes(self):
        engine = TrustEngine(lambda: {"status": "pass"})
        for mode in AutonomyMode:
            policy = self.memory.set_policy(
                autonomy_mode=mode, auto_retry_enabled=True, explicit_user_change=True
            )
            contract = publication_contract(policy)
            self.assertTrue(policy.require_publish_approval)
            self.assertFalse(engine.publish_allowed_without_current_gate(policy))
            self.assertEqual((contract.upload_mode, contract.uploader), ("manual_in_youtube_studio", "user_only"))
            self.assertFalse(channel_os_youtube_upload_allowed(policy))
            self.assertFalse(channel_os_youtube_publish_allowed(policy))

    def test_15_protected_contracts_and_l6_transport_ownership(self):
        expected = {
            "scripts/retry_after_policy.py": "483281935a0907a9f74c571949bc7f122bed2f46",
            "scripts/provider_retry_ownership.py": "ed5b2826105f852eaf3c4f5f2e113d075905498e",
            "scripts/planning_checkpoint_state.py": "d5768abbe19c303339c9d0fad71297ec70527de3",
        }
        for name, expected_sha in expected.items():
            data = Path(name).read_bytes()
            actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            self.assertEqual(actual, expected_sha, name)

        publish_gate = Path("scripts/telegram_publish_gate.py").read_text(encoding="utf-8")
        l6_contract = Path("scripts/orchestration_telegram_ingress_outbox.py").read_text(encoding="utf-8")
        adapter = Path("scripts/channel_os_telegram_adapter.py").read_text(encoding="utf-8")
        webhook = Path("scripts/telegram_webhook_replay.py").read_text(encoding="utf-8")
        self.assertNotIn('"getUpdates"', publish_gate)
        self.assertIn('WEBHOOK = "WEBHOOK"', l6_contract)
        self.assertIn("channel_os_telegram_adapter", webhook)
        for forbidden in ("TELEGRAM_BOT_TOKEN", "getUpdates", "sendMessage", "api.telegram.org", "TelegramClient"):
            self.assertNotIn(forbidden, adapter)

    def test_16_integrated_scenario(self):
        store = EditorialPacketStore(self.tmp.name)
        store.create(packet_id="gate", **FIELDS)
        store.edit("gate", expected_version=1, changes={"hook": "Hook V2"})
        approved = store.approve("gate", expected_version=2)
        request = bind_to_production_request({"request_id": "req"}, approved)
        self.assertTrue(verify_production_binding(request, approved))

        running = MissionControl(self.memory, Provider({"video": live("video", "running", "77")})).snapshot(
            [VideoEntity("video", "Topic", "Ready", packet_version="V2", run_id="77")]
        ).items[0]
        self.assertEqual(running.mission_state, "Producing")

        decision = TrustEngine(lambda: {"status": "pass"}).decide(
            FailureEvidence("transient", transient=True, provider_retry_after=1, wait_budget_seconds=5),
            self.memory.get_policy(),
        )
        self.assertEqual(decision.action, "defer_to_existing_retry_owner")
        self.assertTrue(decision.requires_publish_approval)
        self.assertFalse(channel_os_youtube_upload_allowed(self.memory.get_policy()))

        analysis = ChannelBrain(min_cohort_size=3).analyze(
            perf(9, views=700, ctr=.06, retention=.35),
            [perf(i, views=1000, ctr=.10, retention=.50) for i in range(1, 6)],
        )
        self.assertEqual(analysis.authority, "advisory_only")
        self.assertTrue(analysis.what_should_change_next_time)

        self.memory.upsert_learned_preference(
            preference_id="one", hypothesis="prefers Hook V2", scope="hook", confidence=.4,
            evidence_refs=["decision-v2"], explanation="one decision",
        )
        with self.assertRaises(ValueError):
            self.memory.promote_learned_preference(preference_id="one", key="hook", value="V2")

    def test_17_v1_has_no_mini_app_and_user_can_block_promotion(self):
        self.memory.upsert_learned_preference(
            preference_id="lp", hypothesis="prefers X", scope="topic", confidence=.9,
            evidence_refs=["1", "2", "3"], explanation="pattern",
        )
        self.memory.mark_do_not_promote(preference_id="lp", event_id="corr")
        with self.assertRaises(ValueError):
            self.memory.promote_learned_preference(preference_id="lp", key="topic", value="X")
        source = Path("scripts/channel_os_mission_control.py").read_text(encoding="utf-8")
        self.assertNotIn("web_app", source)
        self.assertNotIn("MiniApp", source)


if __name__ == "__main__":
    unittest.main()
