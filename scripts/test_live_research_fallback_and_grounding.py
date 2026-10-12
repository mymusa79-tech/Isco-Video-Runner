import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from scripts import telegram_clean_v2_control as control
from scripts import tavily_research_lite as sources

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
    def test_existing_idea_response_preserves_optional_knowledge_query(self):
        rows = control._parse_candidate_rows(json.dumps({"candidates": [{
            "title": "لماذا نقارن أنفسنا بالآخرين؟", "market_query": "المقارنة الاجتماعية",
            "reason": "فهم أثر اختيار معيار المقارنة", "knowledge_query_en": "social comparison self evaluation",
        }]}))
        self.assertEqual(rows[0]["knowledge_query_en"], "social comparison self evaluation")

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

    def test_selected_excerpts_reach_each_format_and_are_bound_to_approval(self):
        snippet = (
            "Social comparison and self evaluation were associated in an observational sample of adult volunteers. "
            "This association does not establish causality."
        )
        for scope, fmt in (("short", "short"), ("long", "film"), ("podcast", "podcast")):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                state = self._state()
                state["sessions"]["s1"]["scope"] = scope
                state["ideas"][0]["title"] = "لماذا نقارن أنفسنا بالآخرين؟"
                state["ideas"][0]["knowledge_query_en"] = "social comparison self evaluation"
                post = mock.Mock(return_value={"results": [{
                    "title": "Comparison study", "url": "https://pubmed.ncbi.nlm.nih.gov/fixture", "content": snippet,
                }]})
                def collect(key, topic, **kwargs):
                    return sources.collect_topic_sources(key, topic, post=post, **kwargs)
                with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}), \
                        mock.patch.object(control, "collect_topic_sources", side_effect=collect):
                    request = control.select_candidate(state, "s1", 0)
                self.assertEqual(post.call_count, 1)
                confirmation = control.render_selection_confirmation(request)
                self.assertLess(confirmation.index("Comparison study"), confirmation.index("مصدر يوتيوب"))
                self.assertIn("📊 عينات اهتمام", confirmation)
                digest = request["request_sha256"]
                control.confirm_current(state)
                control.mark_dispatched(state, request["request_id"], digest)
                brief = control.materialize_brief(state, request["request_id"], digest, fmt, Path(tmp) / "brief.json")
                self.assertEqual(brief["research_pack"][1]["source_excerpt"], snippet)
                constraints = "\n".join(brief["hard_constraints"])
                self.assertIn("CONTENT EVIDENCE USE", constraints)
                self.assertIn("section purpose", constraints)
                self.assertIn("shot_intent", constraints)
                self.assertEqual(digest, control._request_hash(request))
                request["research_pack"][1]["source_excerpt"] = "unapproved replacement"
                with self.assertRaisesRegex(RuntimeError, "integrity mismatch"):
                    control.materialize_brief(state, request["request_id"], digest, fmt, Path(tmp) / "tampered.json")

    def test_missing_content_is_disclosed_and_legacy_briefs_remain_unchanged(self):
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": ""}, clear=False):
            request = control.select_candidate(self._state(), "s1", 0)
        text = control.render_selection_confirmation(request)
        self.assertIn("لم يتوفر مقتطف معرفي صالح", text)
        self.assertIn("مصدر يوتيوب", text)
        constraints = "\n".join(control._content_use_constraints(request["research_pack"], "short"))
        self.assertIn("No substantive source excerpt", constraints)
        self.assertEqual(control._content_use_constraints([
            {"source_title": "legacy", "source_url": "https://youtu.be/legacy", "claim_scope": "market"},
        ], "short"), [])


if __name__ == "__main__":
    unittest.main()

