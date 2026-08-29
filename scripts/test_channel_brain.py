import unittest
from scripts.channel_brain import ChannelBrain, VideoPerformance


def perf(i, *, kind="long", topic="focus", duration=480, hour=18, views=1000, ctr=.08, retention=.45):
    return VideoPerformance(
        video_id=f"v{i}", content_type=kind, topic_family=topic, duration_seconds=duration,
        published_at=f"2026-08-{10+i:02d}T{hour:02d}:00:00+00:00", views=views, ctr=ctr, retention=retention,
    )


class ChannelBrainTests(unittest.TestCase):
    def setUp(self):
        self.brain = ChannelBrain(min_cohort_size=3, recent_n=5)

    def test_builds_all_required_comparative_cohorts(self):
        target = perf(9, views=1300, ctr=.10, retention=.52)
        history = [perf(i) for i in range(1, 7)]
        result = self.brain.analyze(target, history)
        names = {x.cohort for x in result.comparisons}
        self.assertEqual(names, {"channel_average", "recent_n", "same_topic", "same_type", "similar_duration", "same_publish_window"})
        self.assertEqual(result.logic_version, "channel-brain-v1")

    def test_long_short_are_never_mixed_in_recommendation_baselines(self):
        target = perf(9, kind="long")
        history = [perf(1, kind="short", duration=30), perf(2, kind="short", duration=40), perf(3, kind="short", duration=50), perf(4), perf(5), perf(6)]
        result = self.brain.analyze(target, history)
        for cohort in result.comparisons:
            self.assertFalse(any(video_id in {"v1", "v2", "v3"} for video_id in cohort.evidence_ids))

    def test_missing_analytics_are_explicit_not_fabricated(self):
        target = perf(9, ctr=None, retention=None)
        result = self.brain.analyze(target, [perf(i) for i in range(1, 5)])
        self.assertEqual(set(result.missing_metrics), {"ctr", "retention"})
        for cohort in result.comparisons:
            ctr = next(x for x in cohort.metrics if x.metric == "ctr")
            self.assertIsNone(ctr.target_value)
            self.assertIsNone(ctr.delta_percent)
            self.assertEqual(ctr.status, "target_metric_unavailable")

    def test_small_sample_never_claims_pattern(self):
        target = perf(9, views=2000, ctr=.2, retention=.8)
        result = self.brain.analyze(target, [perf(1), perf(2)])
        self.assertEqual(result.what_won, ())
        self.assertEqual(result.what_lost, ())
        self.assertIn("Insufficient reliable signal", result.what_should_change_next_time[0])

    def test_outputs_win_loss_and_next_change_without_authority(self):
        history = [perf(i, views=1000, ctr=.10, retention=.50) for i in range(1, 7)]
        target = perf(9, views=700, ctr=.06, retention=.35)
        result = self.brain.analyze(target, history)
        self.assertTrue(any(x.startswith("views underperformed") for x in result.what_lost))
        self.assertTrue(any(x.startswith("ctr underperformed") for x in result.what_lost))
        self.assertTrue(any(x.startswith("retention underperformed") for x in result.what_lost))
        context = result.as_context()
        self.assertEqual(context["authority"], "advisory_only")
        self.assertFalse(context["may_auto_override"])

    def test_publish_window_and_duration_are_independent_cohorts(self):
        target = perf(9, duration=500, hour=18)
        history = [
            perf(1, duration=490, hour=18), perf(2, duration=510, hour=19), perf(3, duration=520, hour=17),
            perf(4, duration=900, hour=2), perf(5, duration=900, hour=3), perf(6, duration=900, hour=4),
        ]
        result = self.brain.analyze(target, history)
        by_name = {x.cohort: x for x in result.comparisons}
        self.assertEqual(set(by_name["similar_duration"].evidence_ids), {"v1", "v2", "v3"})
        self.assertEqual(set(by_name["same_publish_window"].evidence_ids), {"v1", "v2", "v3"})

    def test_target_never_compares_against_itself(self):
        target = perf(9)
        result = self.brain.analyze(target, [target, perf(1), perf(2), perf(3)])
        self.assertTrue(all("v9" not in cohort.evidence_ids for cohort in result.comparisons))


if __name__ == "__main__":
    unittest.main()
