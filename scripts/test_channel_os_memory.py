import json
import tempfile
import unittest
from pathlib import Path

from scripts.channel_os_memory import AutonomyMode, ChannelOSMemory, LiveState


class Provider:
    def __init__(self, state=None, error=None):
        self.state = state
        self.error = error

    def fetch(self, video_id):
        if self.error:
            raise RuntimeError(self.error)
        return self.state


class ChannelOSMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = ChannelOSMemory(self.tmp.name, min_evidence=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_five_layers_are_physically_separated_and_live_has_no_file(self):
        self.mem.set_stable_preference(key="tone", value="warm", explicit_user_statement=True)
        self.mem.append_episode(event_id="e1", video_id="v1", decision_type="hook", selection="b")
        self.mem.set_policy(autonomy_mode=AutonomyMode.BALANCED, auto_retry_enabled=True, explicit_user_change=True)
        self.mem.upsert_learned_preference(
            preference_id="lp1", hypothesis="prefers concise hooks", scope="channel", confidence=.6,
            evidence_refs=["e1"], explanation="one observed decision"
        )
        paths = self.mem.persistent_paths
        self.assertEqual(set(paths), {"stable_preferences", "episodic_decisions", "operational_policies", "learned_preferences"})
        self.assertEqual(len({p.name for p in paths.values()}), 4)
        self.assertFalse(any("live" in p.name for p in paths.values()))
        self.assertTrue(all(p.exists() for p in paths.values()))

    def test_single_sample_cannot_become_stable_preference(self):
        with self.assertRaises(ValueError):
            self.mem.set_stable_preference(key="thumbnail", value="dark", evidence_refs=["one-rejection"])
        learned = self.mem.upsert_learned_preference(
            preference_id="lp", hypothesis="dislikes dark thumbnails", scope="thumbnail", confidence=.4,
            evidence_refs=["one-rejection"], explanation="single event only"
        )
        self.assertEqual(learned.evidence_count, 1)
        with self.assertRaises(ValueError):
            self.mem.promote_learned_preference(preference_id="lp", key="thumbnail", value="avoid-dark")

    def test_explicit_user_statement_may_be_stable_without_inference(self):
        item = self.mem.set_stable_preference(key="language", value="ar", explicit_user_statement=True)
        self.assertEqual(item.provenance, "explicit_user_statement")

    def test_learned_preference_is_explainable(self):
        item = self.mem.upsert_learned_preference(
            preference_id="lp", hypothesis="prefers topic X", scope="topic", confidence=.8,
            evidence_refs=["e1", "e2", "e3"], explanation="3 repeated selections", recency="2026-08-29T00:00:00+00:00"
        )
        explanation = item.explain()
        for field in ("confidence", "evidence_count", "recency", "evidence_refs", "explanation"):
            self.assertIn(field, explanation)
        self.assertEqual(explanation["evidence_count"], 3)

    def test_user_correction_blocks_promotion_and_is_episode(self):
        self.mem.upsert_learned_preference(
            preference_id="lp", hypothesis="prefers X", scope="topic", confidence=.9,
            evidence_refs=["e1", "e2", "e3"], explanation="repeated"
        )
        updated = self.mem.mark_do_not_promote(preference_id="lp", event_id="corr1")
        self.assertEqual(updated.status, "do_not_promote")
        with self.assertRaises(ValueError):
            self.mem.promote_learned_preference(preference_id="lp", key="topic", value="X")
        self.assertEqual(self.mem.episodes()[-1].decision_type, "learned_preference_correction")

    def test_operational_policy_requires_explicit_change(self):
        with self.assertRaises(ValueError):
            self.mem.set_policy(autonomy_mode="autopilot", auto_retry_enabled=True, explicit_user_change=False)
        self.assertEqual(self.mem.get_policy().autonomy_mode, "balanced")

    def test_publish_approval_policy_cannot_be_disabled(self):
        for mode in AutonomyMode:
            with self.assertRaises(ValueError):
                self.mem.set_policy(
                    autonomy_mode=mode, auto_retry_enabled=True,
                    require_publish_approval=False, explicit_user_change=True
                )

    def test_live_state_reads_provider_not_stored_memory(self):
        fake_memory = Path(self.tmp.name) / "live_state.json"
        fake_memory.write_text(json.dumps({"status": "producing"}), encoding="utf-8")
        state = LiveState(
            video_id="v1", status="failed", stage="voice", run_id="10",
            source="github-actions", observed_at="2026-08-29T00:00:00+00:00"
        )
        observed = self.mem.read_live_state(Provider(state=state), "v1")
        self.assertEqual(observed.status, "failed")

    def test_unavailable_live_source_returns_unknown_without_fallback(self):
        observed = self.mem.read_live_state(Provider(error="github unavailable"), "v1")
        self.assertFalse(observed.available)
        self.assertEqual(observed.status, "unknown")
        self.assertIn("unavailable", observed.source)

    def test_episode_log_is_append_only(self):
        self.mem.append_episode(event_id="a", video_id="v", decision_type="x", selection="1")
        self.mem.append_episode(event_id="b", video_id="v", decision_type="x", selection="2")
        self.assertEqual([x.event_id for x in self.mem.episodes()], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
