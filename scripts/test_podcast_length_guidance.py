import unittest

from clean_v2 import providers
from clean_v2.pipeline import (
    PODCAST_MIN_ESTIMATED_WORDS,
    _podcast_word_guidance,
    _route_script_with_single_podcast_length_repair,
    _script_prompt,
)


def _brief(fmt):
    return {
        "approved_by_user": True,
        "approved_topic": "لماذا نعرف النصيحة الصحيحة ولا يتغير سلوكنا؟",
        "format": fmt,
        "language": "ar",
        "research_pack": [],
    }


def _plan():
    return {
        "title": "t", "promise": "p", "cta": "c", "narrative_format": "dialogue_qa",
        "sections": [{"id": f"s{i}", "heading": "h", "purpose": "p", "visual_query_en": "q"} for i in (1, 2, 3)],
    }


class PodcastWordGuidanceTests(unittest.TestCase):
    def test_floor_is_four_minutes_in_words(self):
        self.assertEqual(PODCAST_MIN_ESTIMATED_WORDS, 420)

    def test_podcast_prompt_has_derived_word_guidance_other_formats_do_not(self):
        podcast = _script_prompt(_brief("podcast"), _plan())
        self.assertIn(_podcast_word_guidance(3), podcast)
        self.assertIn("never as a target to pad toward", podcast)
        for fmt in ("film", "short"):
            try:
                other = _script_prompt(_brief(fmt), _plan())
            except Exception:
                continue
            self.assertNotIn(_podcast_word_guidance(3), other)

    def test_guidance_follows_the_plan_not_fixed_numbers(self):
        three, five = _podcast_word_guidance(3), _podcast_word_guidance(5)
        self.assertIn("about 140 words per section", three)
        self.assertIn("about 84 words per section", five)
        self.assertNotEqual(three, five)
        plan5 = _plan()
        plan5["sections"] = plan5["sections"] + [dict(plan5["sections"][0], id="s4"), dict(plan5["sections"][0], id="s5")]
        self.assertIn(_podcast_word_guidance(5), _script_prompt(_brief("podcast"), plan5))

    def test_repair_prompt_reports_word_counts_and_floor(self):
        class R:
            def __init__(self, c): self.c, self.calls = list(c), []
            def route(self, **kw):
                self.calls.append(kw); return kw["validator"](self.c.pop(0))
        short = {"sections": [{"id": "s1", "narration": "A: سؤال B: " + ("فكرة " * 150)}]}
        deep = {"sections": [{"id": "s1", "narration": "A: سؤال B: " + ("فكرة " * 500)}]}
        r = R([short, deep])
        _route_script_with_single_podcast_length_repair(
            router=r, fmt="podcast", prompt="BASE", max_tokens=1, validator=lambda v: v)
        p = r.calls[1]["prompt"]
        self.assertIn("(151 words)", p)
        self.assertIn("floor of 420 words", p)
        self.assertIn(_podcast_word_guidance(1), p)

    def test_too_short_rewrite_error_carries_measures(self):
        class R:
            def __init__(self): self.n = 0
            def route(self, **kw):
                s = {"sections": [{"id": "s1", "narration": "A: س B: " + ("فكرة " * 100)}]}
                if self.n == 0:
                    self.n += 1
                    return kw["validator"](s)
                return kw["validator"](s)
        with self.assertRaises(RuntimeError) as ctx:
            _route_script_with_single_podcast_length_repair(
                router=R(), fmt="podcast", prompt="B", max_tokens=1, validator=lambda v: v)
        msg = str(ctx.exception)
        self.assertTrue(providers._SAFE_VALIDATOR_MEASURE_RE.fullmatch(msg), msg)
        self.assertIn("words=101", msg)
        self.assertIn("minimum_words=420", msg)


class ScriptValidatorDetailTests(unittest.TestCase):
    def _events(self, exc):
        def fake(prompt, max_tokens, stage):
            return {"x": 1}
        def val(c):
            raise exc
        r = providers.ProviderRouter((providers.ProviderAdapter("mistral", fake, accepts_stage=True),))
        with self.assertRaises(Exception):
            r.route(stage="script", prompt="p", max_tokens=1, validator=val)
        return [e for e in r.events if e["result"] == "invalid_output"]

    def test_safe_measure_message_is_persisted(self):
        ev = self._events(RuntimeError(
            "podcast_estimated_duration_too_short estimated_seconds=210.5 minimum=240.0 words=367 minimum_words=420"))
        self.assertIn("words=367", ev[0]["detail"])

    def test_message_quoting_content_is_not_persisted(self):
        ev = self._events(RuntimeError("bad section: هذا نص من السرد يجب ألا يُحفظ"))
        self.assertIsNone(ev[0]["detail"])
        ev = self._events(RuntimeError("podcast_x words=3 hello world"))
        self.assertIsNone(ev[0]["detail"])


if __name__ == "__main__":
    unittest.main()
