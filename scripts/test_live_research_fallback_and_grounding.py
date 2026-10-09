import os
import unittest
import urllib.error
from unittest import mock

from scripts import telegram_clean_v2_control as control

ROWS_JSON = '{"candidates":[{"title":"فكرة جديدة","market_query":"عادات يومية","reason":"سبب"}]}'


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


class LiveResearchFallback(unittest.TestCase):
    def setUp(self):
        control._RESEARCH_FAILURES.clear()

    def test_gemini_503_retries_then_falls_back_to_mistral(self):
        env = {"GEMINI_API_KEY": "g", "MISTRAL_API_KEY": "m"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(control.time, "sleep"), \
                mock.patch.object(control, "_json_request") as req:
            def fake(url, **kw):
                if "generativelanguage" in url:
                    raise _http(503)
                return {"choices": [{"message": {"content": ROWS_JSON}}]}
            req.side_effect = fake
            rows = control._gemini_candidates([], "short")
        self.assertEqual(rows[0]["title"], "فكرة جديدة")
        self.assertNotIn("gemini_unavailable", control._RESEARCH_FAILURES)
        self.assertEqual(sum("generativelanguage" in c.args[0] for c in req.call_args_list), 2)

    def test_all_providers_failing_records_reason(self):
        env = {"GEMINI_API_KEY": "g", "MISTRAL_API_KEY": "m", "OPENROUTER_API_KEY": "o"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(control.time, "sleep"), \
                mock.patch.object(control, "_json_request", side_effect=_http(429)):
            rows = control._gemini_candidates([], "short")
        self.assertEqual(rows, [])
        self.assertIn("gemini_unavailable", control._RESEARCH_FAILURES)

    def test_gemini_success_does_not_touch_fallbacks(self):
        env = {"GEMINI_API_KEY": "g", "MISTRAL_API_KEY": "m"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(control, "_json_request") as req:
            req.return_value = {"candidates": [{"content": {"parts": [{"text": ROWS_JSON}]}}]}
            rows = control._gemini_candidates([], "short")
        self.assertEqual(len(rows), 1)
        self.assertEqual(req.call_count, 1)


class SelectionGrounding(unittest.TestCase):
    def _state(self):
        state = control.default_state()
        state["ideas"].append({
            "idea_id": "idea-1", "title": "موضوع جديد", "normalized_title": "موضوع جديد",
            "market_query": "q", "reason": "r", "market_score": 0.8,
            "market_evidence": {"sample_count": 1, "distinct_channels": 1, "top_samples": [
                {"video_id": "vid", "title": "مصدر يوتيوب", "channel": "c",
                 "published_at": "2026-09-20T00:00:00Z", "views": 1000, "views_per_day": 250}]},
            "research_pack": [], "selected": False, "created_at": control.utc_now(),
        })
        state["sessions"]["s1"] = {"session_id": "s1", "scope": "short", "idea_ids": ["idea-1"],
                                   "created_at": control.utc_now()}
        return state

    def test_web_sources_are_appended_after_youtube_evidence(self):
        web = [{"source_title": "Web", "source_url": "https://a.org/x", "claim_scope": "مقتطف"}]
        with mock.patch.object(control, "collect_topic_sources", return_value=web):
            request = control.select_candidate(self._state(), "s1", 0)
        titles = [x["source_title"] for x in request["research_pack"]]
        self.assertEqual(titles, ["مصدر يوتيوب", "Web"])

    def test_no_tavily_keeps_youtube_only_pack(self):
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": ""}, clear=False):
            request = control.select_candidate(self._state(), "s1", 0)
        self.assertEqual(len(request["research_pack"]), 1)


if __name__ == "__main__":
    unittest.main()
