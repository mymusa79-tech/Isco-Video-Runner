import tempfile
import unittest

from scripts.channel_os_memory import ChannelOSMemory, LiveState
from scripts.channel_os_mission_control import MISSION_STATES, MissionControl, VideoEntity, render_telegram


class Provider:
    def __init__(self, mapping=None, failures=None):
        self.mapping = mapping or {}
        self.failures = failures or set()
        self.calls = []

    def fetch(self, video_id):
        self.calls.append(video_id)
        if video_id in self.failures:
            raise RuntimeError("github unavailable")
        return self.mapping[video_id]


def live(video_id, status, run="1", reason=""):
    return LiveState(video_id=video_id, status=status, stage="production", run_id=run, source="github-actions", observed_at="2026-08-29T00:00:00+00:00", reason=reason)


class MissionControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = ChannelOSMemory(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_portfolio_has_all_seven_states(self):
        provider = Provider({
            "prod": live("prod", "running", "10"),
            "need": live("need", "action_required", "11", "editorial choice"),
            "bad": live("bad", "failed", "12", "provider exhausted"),
        })
        entities = [
            VideoEntity("idea", "Idea", "Ideas"), VideoEntity("ready", "Ready", "Ready"),
            VideoEntity("prod", "Prod", "Ready", run_id="10"), VideoEntity("need", "Need", "Ready", run_id="11"),
            VideoEntity("sched", "Scheduled", "Scheduled"), VideoEntity("pub", "Published", "Published"),
            VideoEntity("bad", "Bad", "Ready", run_id="12"),
        ]
        snapshot = MissionControl(self.memory, provider).snapshot(entities)
        self.assertEqual(set(snapshot.counts), set(MISSION_STATES))
        self.assertTrue(all(snapshot.counts[name] == 1 for name in MISSION_STATES))

    def test_stale_durable_status_never_beats_live_failure(self):
        provider = Provider({"v": live("v", "failed", "99", "live failure")})
        entity = VideoEntity("v", "Video", "Ready", run_id="99")
        item = MissionControl(self.memory, provider).snapshot([entity]).items[0]
        self.assertEqual(item.mission_state, "Problems")
        self.assertEqual(item.source, "github-actions")

    def test_source_unavailable_is_unknown_problem_not_cached_fallback(self):
        provider = Provider(failures={"v"})
        entity = VideoEntity("v", "Video", "Ready", run_id="99")
        snapshot = MissionControl(self.memory, provider).snapshot([entity])
        self.assertEqual(snapshot.items[0].mission_state, "Problems")
        self.assertEqual(snapshot.items[0].source, "live-source-unavailable")
        self.assertEqual(snapshot.source_unavailable_count, 1)

    def test_no_run_binding_does_not_issue_fake_live_read(self):
        provider = Provider()
        entity = VideoEntity("v", "Idea", "Ideas")
        item = MissionControl(self.memory, provider).snapshot([entity]).items[0]
        self.assertEqual(item.mission_state, "Ideas")
        self.assertEqual(provider.calls, [])

    def test_successful_run_does_not_imply_published(self):
        provider = Provider({"v": live("v", "success", "2")})
        item = MissionControl(self.memory, provider).snapshot([VideoEntity("v", "Done", "Ready", run_id="2")]).items[0]
        self.assertEqual(item.mission_state, "Ready")

    def test_scheduled_and_published_require_durable_publication_state(self):
        provider = Provider({"s": live("s", "success", "3"), "p": live("p", "success", "4")})
        snapshot = MissionControl(self.memory, provider).snapshot([
            VideoEntity("s", "S", "Scheduled", run_id="3"), VideoEntity("p", "P", "Published", run_id="4")
        ])
        self.assertEqual([x.mission_state for x in snapshot.items], ["Scheduled", "Published"])

    def test_telegram_render_is_text_only_and_actionable(self):
        provider = Provider({"v": live("v", "failed", "9", "unsafe state")})
        snapshot = MissionControl(self.memory, provider).snapshot([VideoEntity("v", "Video", "Ready", run_id="9")])
        text, keyboard = render_telegram(snapshot)
        self.assertIn("Mission Control", text)
        self.assertIn("Problems: 1", text)
        self.assertIn("unsafe state", text)
        self.assertTrue(keyboard)
        self.assertNotIn("web_app", str(keyboard))


if __name__ == "__main__":
    unittest.main()
