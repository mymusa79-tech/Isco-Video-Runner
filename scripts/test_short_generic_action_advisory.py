import unittest

from clean_v2.pipeline import _short_generic_action_only_advisory as adv

FLAG = "content_depth:s3 practical_action_generic: payoff generic action not tied to earlier detail."


def base(**kw):
    r = {
        "status": "block", "narrative_format_flags": [FLAG],
        "cultural_dignity_flags": [], "filler_flags": [], "naturalness_flags": [],
        "preachiness_flags": [], "unverified_religious_quote_flags": [],
        "hook_body_continuity": True, "hook_curiosity": True, "hook_honesty": True,
        "hook_specificity": True, "hook_genericness": False, "payoff_resolves_hook": True,
        "section_dependency": True, "topic_fidelity": True,
        "editorial_promise_continuity": {"decision": "pass"},
        "viewer_retention_continuity": {"decision": "pass"},
    }
    r.update(kw)
    return r


class ShortGenericActionAdvisoryTests(unittest.TestCase):
    plan = {"short_template": "micro_story"}

    def test_only_generic_action_becomes_advisory(self):
        out = adv(base(), self.plan)
        self.assertEqual(out["status"], "pass")
        self.assertEqual(out["advisory_narrative_format_flags"], [FLAG])
        self.assertEqual(out["narrative_format_flags"], [])

    def test_any_other_problem_stays_blocked(self):
        for extra in (
            {"hook_specificity": False},
            {"naturalness_flags": ["x"]},
            {"narrative_format_flags": [FLAG, "hook_quality: generic"]},
            {"viewer_retention_continuity": {"decision": "block"}},
            {"unverified_religious_quote_flags": ["q"]},
        ):
            with self.subTest(extra=extra):
                self.assertEqual(adv(base(**extra), self.plan)["status"], "block")

    def test_not_applied_outside_short(self):
        self.assertEqual(adv(base(), {})["status"], "block")


class DeterministicAlternateTests(unittest.TestCase):
    def test_same_subject_zero_call_alternate(self):
        from clean_v2.visual_qa import _deterministic_broadened_alternate as f
        original = "hands holding a completed puzzle piece fitting perfectly into place"
        out = f(original)
        self.assertIn("puzzle", out)
        self.assertIn("no face", out)
        self.assertNotEqual(out.lower(), original.lower())
        self.assertEqual(f("paper"), "")


if __name__ == "__main__":
    unittest.main()
