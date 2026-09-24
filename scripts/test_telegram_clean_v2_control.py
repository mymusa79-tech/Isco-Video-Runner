from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import telegram_clean_v2_control as control


class TelegramCleanV2ControlTests(unittest.TestCase):
    def test_same_topic_is_conservative_across_arabic_variants(self):
        self.assertTrue(
            control.same_topic(
                "كيف تستعيد تركيزك بعد أيام من التشتت؟",
                "كيف استعيد التركيز بعد ايام من التشتت",
            )
        )
        self.assertFalse(
            control.same_topic(
                "كيف تستعيد تركيزك بعد أيام من التشتت؟",
                "كيف تبني عادة تستمر عندما يختفي الدافع؟",
            )
        )

    def test_research_saves_selected_and_unselected_candidates(self):
        state = control.default_state()
        rows = [
            {"title": title, "market_query": query, "reason": "سبب"}
            for title, query in [
                ("كيف تستعيد تركيزك بعد يوم مشتت؟", "استعادة التركيز"),
                ("لماذا نفقد الحماس بعد بداية قوية؟", "فقدان الحماس"),
                ("كيف تبني عادة تستمر دون دافع؟", "بناء العادات"),
                ("متى تتحول الراحة إلى هروب؟", "الراحة والهروب"),
                ("كيف تنهي ما بدأت قبل مشروع جديد؟", "إنهاء المشاريع"),
                ("لماذا تجعل كثرة الخيارات القرار أصعب؟", "كثرة الخيارات"),
            ]
        ]
        with mock.patch.object(control, "_candidate_pool", return_value=rows), mock.patch.object(
            control,
            "market_evidence",
            side_effect=[
                (0.9 - i * 0.05, {
                    "sample_count": 3,
                    "distinct_channels": 3,
                    "max_views_per_day": 1000 - i * 10,
                    "median_views_per_day": 500 - i * 5,
                    "top_samples": [
                        {
                            "video_id": f"v{i}",
                            "title": f"مصدر {i}",
                            "channel": "قناة",
                            "published_at": "2026-09-20T00:00:00Z",
                            "views": 1000,
                            "views_per_day": 250,
                        }
                    ],
                })
                for i in range(6)
            ],
        ):
            result = control.research(state, "long")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(len(state["ideas"]), 6)
        self.assertTrue(all(item["selected"] is False for item in state["ideas"]))
        self.assertTrue(all(item["research_pack"] == [] for item in result["candidates"]))
        selected = control.select_candidate(state, result["session_id"], 0)
        self.assertTrue(selected["research_pack"])
        self.assertIn("source_title", selected["research_pack"][0])

    def test_selection_does_not_dispatch_and_confirmation_is_separate(self):
        state = control.default_state()
        idea = {
            "idea_id": "idea-1",
            "title": "موضوع جديد واضح ومفيد",
            "normalized_title": "موضوع جديد واضح ومفيد",
            "market_query": "موضوع جديد",
            "reason": "سبب",
            "market_score": 0.8,
            "market_evidence": {
                "sample_count": 1,
                "distinct_channels": 1,
                "top_samples": [
                    {
                        "video_id": "example",
                        "title": "مصدر",
                        "channel": "قناة",
                        "published_at": "2026-09-20T00:00:00Z",
                        "views": 1000,
                        "views_per_day": 250,
                    }
                ],
            },
            "research_pack": [],
            "selected": False,
            "created_at": control.utc_now(),
        }
        state["ideas"].append(idea)
        state["sessions"]["s1"] = {
            "session_id": "s1",
            "scope": "bundle",
            "idea_ids": ["idea-1"],
            "created_at": control.utc_now(),
        }
        request = control.select_candidate(state, "s1", 0)
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertIsNone(request["dispatched_at"])
        confirmed = control.confirm_current(state)["request"]
        self.assertEqual(confirmed["status"], "confirmed_pending_dispatch")
        self.assertIsNone(confirmed["dispatched_at"])

    def test_request_hash_is_stable_across_lifecycle_changes(self):
        request = {
            "schema_version": 1,
            "request_id": "req-1",
            "source": "clean_v2_telegram_editorial_lite",
            "scope": "long",
            "approved_by_user": True,
            "approved_topic": "موضوع",
            "research_pack": [],
            "idea_id": "idea-1",
            "selected_at": control.utc_now(),
            "status": "awaiting_confirmation",
            "confirmed_at": None,
            "dispatched_at": None,
        }
        before = control._request_hash(request)
        request["status"] = "dispatched"
        request["confirmed_at"] = control.utc_now()
        request["dispatched_at"] = control.utc_now()
        self.assertEqual(before, control._request_hash(request))

    def test_materialize_requires_dispatched_request_and_enforces_scope(self):
        state = control.default_state()
        request = {
            "schema_version": 1,
            "request_id": "req-1",
            "source": "clean_v2_telegram_editorial_lite",
            "scope": "short",
            "approved_by_user": True,
            "approved_topic": "موضوع",
            "research_pack": [],
            "idea_id": "idea-1",
            "selected_at": control.utc_now(),
            "status": "dispatched",
            "confirmed_at": control.utc_now(),
            "dispatched_at": control.utc_now(),
        }
        request["request_sha256"] = control._request_hash(request)
        state["requests"]["req-1"] = request
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "brief.json"
            brief = control.materialize_brief(
                state,
                "req-1",
                request["request_sha256"],
                "short",
                output,
            )
            self.assertEqual(brief["format"], "short")
            self.assertTrue(output.is_file())
            with self.assertRaises(RuntimeError):
                control.materialize_brief(
                    state,
                    "req-1",
                    request["request_sha256"],
                    "film",
                    output,
                )

    def test_exact_confirmation_text_only_creates_dispatch_file(self):
        state = control.default_state()
        request = {
            "schema_version": 1,
            "request_id": "req-1",
            "source": "clean_v2_telegram_editorial_lite",
            "scope": "long",
            "approved_by_user": True,
            "approved_topic": "موضوع",
            "research_pack": [],
            "idea_id": "idea-1",
            "selected_at": control.utc_now(),
            "status": "awaiting_confirmation",
            "confirmed_at": None,
            "dispatched_at": None,
        }
        request["request_sha256"] = control._request_hash(request)
        state["requests"]["req-1"] = request
        state["current_request_id"] = "req-1"
        update = {"message": {"from": {"id": 123}, "chat": {"id": 123}, "text": "تأكيد الإنتاج"}}
        almost = {"message": {"from": {"id": 123}, "chat": {"id": 123}, "text": "تأكيد الانتاج"}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ",
            {"TELEGRAM_CHAT_ID": "123"},
            clear=False,
        ), mock.patch.object(control, "send_telegram"):
            dispatch = Path(tmp) / "dispatch.json"
            control.handle_update(state, almost, dispatch)
            self.assertFalse(dispatch.exists())
            control.handle_update(state, update, dispatch)
            self.assertTrue(dispatch.exists())
            payload = json.loads(dispatch.read_text(encoding="utf-8"))
            self.assertEqual(payload["request_id"], "req-1")


if __name__ == "__main__":
    unittest.main()
