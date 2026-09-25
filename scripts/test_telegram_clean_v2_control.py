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
            self.assertFalse(any("30 seconds" in item for item in brief["hard_constraints"]))
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


    def test_duration_parser_supports_youtube_iso8601(self):
        self.assertEqual(control._parse_duration_seconds("PT59S"), 59)
        self.assertEqual(control._parse_duration_seconds("PT2M30S"), 150)
        self.assertEqual(control._parse_duration_seconds("PT1H2M3S"), 3723)

    def test_channel_stats_uses_real_channel_snapshot_deltas(self):
        state = control.default_state()
        state["youtube_snapshots"] = [
            {
                "captured_at": "2026-09-17T09:59:00Z",
                "total_views": 1000,
                "subscribers": 15,
            },
            {
                "captured_at": "2026-09-23T20:00:00Z",
                "total_views": 1300,
                "subscribers": 18,
            },
        ]
        current = {
            "captured_at": "2026-09-24T10:00:00Z",
            "channel_id": "channel",
            "subscribers": 20,
            "hidden_subscribers": False,
            "total_views": 1500,
            "video_count": 12,
            "last_long": {
                "video_id": "long1",
                "title": "آخر حلقة",
                "published_at": "2026-09-23T10:00:00Z",
                "duration_seconds": 240,
                "views": 300,
                "likes": 20,
                "comments": 4,
            },
            "last_short": {
                "video_id": "short1",
                "title": "آخر شورت",
                "published_at": "2026-09-24T08:00:00Z",
                "duration_seconds": 35,
                "views": 800,
                "likes": 70,
                "comments": 5,
            },
        }
        with mock.patch.object(control, "fetch_channel_snapshot", return_value=current):
            stats = control.channel_stats(state)
        self.assertEqual(stats["views_today"], 200)
        self.assertEqual(stats["views_7d"], 500)
        self.assertEqual(stats["subscribers_today"], 2)
        self.assertEqual(stats["subscribers_7d"], 5)
        self.assertEqual(state["youtube_snapshots"][-1]["total_views"], 1500)
        rendered = control.render_channel_stats(stats)
        self.assertIn("مشاهدات اليوم: +200", rendered)
        self.assertIn("مشاهدات آخر 7 أيام: +500", rendered)
        self.assertIn("آخر حلقة", rendered)
        self.assertIn("آخر شورت", rendered)

    def test_stats_command_is_read_only_for_production_dispatch(self):
        state = control.default_state()
        update = {
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123},
                "text": "/stats",
            }
        }
        stats = {
            "captured_at": "2026-09-24T10:00:00Z",
            "channel_id": "channel",
            "subscribers": 20,
            "hidden_subscribers": False,
            "total_views": 1500,
            "video_count": 12,
            "last_long": None,
            "last_short": None,
            "views_today": None,
            "views_7d": None,
            "subscribers_today": None,
            "subscribers_7d": None,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ",
            {"TELEGRAM_CHAT_ID": "123"},
            clear=False,
        ), mock.patch.object(control, "channel_stats", return_value=stats), mock.patch.object(
            control, "send_telegram"
        ) as send:
            dispatch = Path(tmp) / "dispatch.json"
            control.handle_update(state, update, dispatch)
            self.assertFalse(dispatch.exists())
            self.assertTrue(send.called)


    def test_research_returns_up_to_three_qualified_topics(self):
        state = control.default_state()
        rows = [
            {"title": "فكرة أولى جديدة", "market_query": "فكرة أولى", "reason": "سبب"},
            {"title": "فكرة ثانية جديدة", "market_query": "فكرة ثانية", "reason": "سبب"},
        ]
        evidence = {
            "sample_count": 2,
            "distinct_channels": 2,
            "max_views_per_day": 300,
            "median_views_per_day": 200,
            "top_samples": [{"video_id": "v1", "title": "مصدر", "channel": "قناة"}],
        }
        with mock.patch.object(control, "_candidate_pool", return_value=rows), mock.patch.object(
            control, "market_evidence", return_value=(0.7, evidence)
        ):
            result = control.research(state, "long")
        self.assertEqual(len(result["candidates"]), 2)
        rendered, buttons = control.render_candidates(result)
        self.assertIn("2 فكرتان", rendered)
        self.assertEqual(len(buttons), 2)

    def test_research_rejects_only_when_zero_topics_qualify(self):
        state = control.default_state()
        rows = [{"title": "فكرة بلا دليل", "market_query": "فكرة", "reason": "سبب"}]
        with mock.patch.object(control, "_candidate_pool", return_value=rows), mock.patch.object(
            control,
            "market_evidence",
            return_value=(0.0, {"sample_count": 0, "distinct_channels": 0, "top_samples": []}),
        ):
            with self.assertRaisesRegex(RuntimeError, "no unused evidence-backed"):
                control.research(state, "short")

    def test_today_stats_accept_snapshot_five_minutes_after_midnight(self):
        midnight = control._parse_utc("2026-09-23T20:00:00Z")
        snapshots = [
            {"captured_at": "2026-09-23T20:05:00Z", "total_views": 1300, "subscribers": 18}
        ]
        baseline = control._midnight_baseline_snapshot(snapshots, midnight)
        self.assertIsNotNone(baseline)
        self.assertEqual(baseline["total_views"], 1300)

    def test_stats_format_split_uses_clean_v2_short_contract(self):
        videos = [
            {"title": "فيديو 2:50", "duration_seconds": 170, "published_at": "2026-09-24T10:00:00Z"},
            {"title": "شورت 59 ثانية", "duration_seconds": 59, "published_at": "2026-09-24T09:00:00Z"},
        ]
        short, long = control._latest_by_clean_v2_format(videos)
        self.assertEqual(short["title"], "شورت 59 ثانية")
        self.assertEqual(long["title"], "فيديو 2:50")

    def test_research_session_closes_after_first_selection(self):
        state = control.default_state()
        state["ideas"] = [{
            "idea_id": "i1",
            "title": "موضوع واحد",
            "market_evidence": {
                "sample_count": 1,
                "distinct_channels": 1,
                "top_samples": [{"video_id": "v1", "title": "مصدر"}],
            },
            "research_pack": [],
            "selected": False,
        }]
        state["sessions"]["s1"] = {
            "session_id": "s1",
            "scope": "long",
            "idea_ids": ["i1"],
            "created_at": control.utc_now(),
        }
        control.select_candidate(state, "s1", 0)
        self.assertIsNotNone(state["sessions"]["s1"]["closed_at"])
        with self.assertRaisesRegex(RuntimeError, "closed"):
            control.select_candidate(state, "s1", 0)

    def test_cancel_only_cancels_unconfirmed_selection(self):
        state = control.default_state()
        request = {
            "schema_version": 1,
            "request_id": "req-cancel",
            "source": "clean_v2_telegram_editorial_lite",
            "scope": "long",
            "approved_by_user": True,
            "approved_topic": "موضوع قابل للإلغاء",
            "research_pack": [],
            "idea_id": "idea-1",
            "selected_at": control.utc_now(),
            "status": "awaiting_confirmation",
            "confirmed_at": None,
            "dispatched_at": None,
        }
        request["request_sha256"] = control._request_hash(request)
        state["requests"][request["request_id"]] = request
        state["current_request_id"] = request["request_id"]
        cancelled = control.cancel_current(state)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(state["current_request_id"])
        with self.assertRaises(RuntimeError):
            control.cancel_current(state)

    def test_start_opens_one_nested_main_menu(self):
        state = control.default_state()
        update = {"message": {"from": {"id": 123}, "chat": {"id": 123}, "text": "/start"}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"TELEGRAM_CHAT_ID": "123"}, clear=False
        ), mock.patch.object(control, "send_telegram") as send:
            control.handle_update(state, update, Path(tmp) / "dispatch.json")
        text, keyboard = send.call_args.args
        self.assertIn("🏠 الرئيسية", text)
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertEqual(
            callbacks,
            [
                "main:research",
                "main:saved",
                "main:used",
                "main:stats",
                "main:status",
                "main:last",
                "main:cancel",
            ],
        )
        self.assertNotIn("scope:long", callbacks)

    def test_research_copy_uses_human_market_language(self):
        result = {
            "session_id": "s1",
            "candidates": [{
                "title": "موضوع",
                "reason": "سبب واضح",
                "market_evidence": {"sample_count": 6, "distinct_channels": 3},
            }],
        }
        rendered, _ = control.render_candidates(result)
        self.assertIn("وجدنا 6 فيديوهات", rendered)
        self.assertIn("3 قنوات مختلفة", rendered)
        self.assertNotIn("عينات", rendered)

    def test_selection_confirmation_shows_two_sources_before_confirmation(self):
        request = {
            "scope": "long",
            "approved_topic": "موضوع",
            "research_pack": [
                {"source_title": "مصدر أول", "source_url": "https://youtu.be/1"},
                {"source_title": "مصدر ثان", "source_url": "https://youtu.be/2"},
                {"source_title": "مصدر ثالث", "source_url": "https://youtu.be/3"},
            ],
        }
        text = control.render_selection_confirmation(request)
        self.assertIn("مصدر أول", text)
        self.assertIn("https://youtu.be/1", text)
        self.assertIn("مصدر ثان", text)
        self.assertNotIn("مصدر ثالث", text)
        self.assertIn(control.CONFIRM_TEXT, text)

    def test_stats_copy_is_plain_arabic_without_internal_terms(self):
        stats = {
            "subscribers": 20,
            "hidden_subscribers": False,
            "total_views": 1500,
            "video_count": 12,
            "last_long": None,
            "last_short": None,
            "views_today": None,
            "views_7d": None,
            "subscribers_today": None,
            "subscribers_7d": None,
        }
        text = control.render_channel_stats(stats)
        self.assertIn("إحصائيات قناة نداء اليقظة", text)
        self.assertIn("بانتظار أول قياس يومي", text)
        self.assertNotIn("Channel Intelligence", text)
        self.assertNotIn("baseline", text)
        self.assertNotIn("snapshots", text)
        self.assertNotIn("OAuth", text)

    def test_status_command_reports_type_and_real_last_stage(self):
        runtime = {
            "active": True,
            "scope": "bundle",
            "kind": "short",
            "topic": "موضوع",
            "stage": "⚡ الشورت · 3/6 الصوت ✅",
            "run_url": "https://github.example/run/6",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.json"
            path.write_text(json.dumps(runtime, ensure_ascii=False), encoding="utf-8")
            state = control.default_state()
            update = {"message": {"from": {"id": 123}, "chat": {"id": 123}, "text": "/status"}}
            with mock.patch.dict(
                "os.environ",
                {"TELEGRAM_CHAT_ID": "123", "TELEGRAM_RUNTIME_STATE_PATH": str(path)},
                clear=False,
            ), mock.patch.object(control, "send_telegram") as send:
                control.handle_update(state, update, Path(tmp) / "dispatch.json")
            text = send.call_args.args[0]
            self.assertIn("يوجد إنتاج يعمل الآن", text)
            self.assertIn("الشورت", text)
            self.assertIn("3/6 الصوت", text)

    def test_last_command_returns_unified_release_asset_link(self):
        delivery = {
            "kind": "short",
            "topic": "موضوع ناجح",
            "release_tag": "clean-v2-final-short-short-final-one-9-a1",
            "browser_download_url": "https://github.example/releases/download/tag/final.mp4",
        }
        text, keyboard = control.render_last_success(delivery)
        self.assertIn("آخر إنتاج ناجح", text)
        self.assertIn("شورت", text)
        self.assertIn("موضوع ناجح", text)
        self.assertEqual(
            keyboard[0][0]["url"],
            "https://github.example/releases/download/tag/final.mp4",
        )
        self.assertEqual(keyboard[0][0]["text"], "🎥 مشاهدة/تحميل الفيديو")

    def test_latest_delivery_comes_from_clean_v2_release_asset_not_runtime_artifact(self):
        payload = [
            {
                "draft": False,
                "tag_name": "clean-v2-final-short-short-final-one-9-a1",
                "name": "Clean V2 Short — موضوع ناجح",
                "assets": [
                    {
                        "name": "final.mp4",
                        "browser_download_url": "https://github.example/releases/download/tag/final.mp4",
                    }
                ],
            }
        ]
        with mock.patch.object(control, "_github_release_json", return_value=payload):
            delivery = control.latest_release_delivery()
        self.assertEqual(delivery["kind"], "short")
        self.assertEqual(delivery["topic"], "موضوع ناجح")
        self.assertEqual(
            delivery["browser_download_url"],
            "https://github.example/releases/download/tag/final.mp4",
        )
        self.assertNotIn("artifact_url", delivery)

    def test_bundle_research_prompt_requires_long_and_derived_short_fit(self):
        instruction = control._scope_research_instruction("bundle")
        self.assertIn("حلقة طويلة", instruction)
        self.assertIn("شورت قوي من نفس الحلقة", instruction)
        self.assertNotIn("شورت مستقل", instruction)
        self.assertEqual(control.FORMATS_BY_SCOPE["bundle"], ["film"])
        self.assertNotEqual(instruction, control._scope_research_instruction("long"))

    def test_new_research_obsoletes_previous_unselected_results(self):
        state = control.default_state()
        state["sessions"]["old"] = {
            "session_id": "old",
            "scope": "long",
            "idea_ids": [],
            "created_at": control.utc_now(),
        }
        rows = [{"title": "فكرة جديدة كليًا", "market_query": "فكرة جديدة", "reason": "سبب"}]
        evidence = {
            "sample_count": 1,
            "distinct_channels": 1,
            "top_samples": [{"video_id": "v1", "title": "مصدر", "channel": "قناة"}],
        }
        with mock.patch.object(control, "_candidate_pool", return_value=rows), mock.patch.object(
            control, "market_evidence", return_value=(0.8, evidence)
        ):
            result = control.research(state, "short")
        self.assertIn("obsolete_at", state["sessions"]["old"])
        self.assertNotEqual(result["session_id"], "old")


    def test_arabic_keyboard_labels_normalize_to_controller_commands(self):
        cases = {
            "🔎 بحث جديد": "بحث جديد",
            "📊 الإحصائيات": "الإحصائيات",
            "📚 المحفوظات": "المحفوظات",
            "✅ المستعملة": "المستعملة",
            "🟢 حالة الإنتاج": "حالة الإنتاج",
            "🎥 آخر إنتاج": "آخر إنتاج",
            "❌ إلغاء الاختيار": "إلغاء الاختيار",
            "🏠 الرئيسية": "الرئيسية",
            "\u200f📊\ufe0f  الإحصائيات\u200e": "الإحصائيات",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(control._normalize_user_command_text(raw), expected)

    def test_arabic_stats_button_routes_to_stats_instead_of_fallback(self):
        state = control.default_state()
        update = {
            "message": {
                "from": {"id": 123},
                "chat": {"id": 123},
                "text": "📊 الإحصائيات",
            }
        }
        stats = {
            "subscribers": 20,
            "hidden_subscribers": False,
            "total_views": 1500,
            "video_count": 12,
            "last_long": None,
            "last_short": None,
            "views_today": None,
            "views_7d": None,
            "subscribers_today": None,
            "subscribers_7d": None,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"TELEGRAM_CHAT_ID": "123"}, clear=False
        ), mock.patch.object(control, "channel_stats", return_value=stats), mock.patch.object(
            control, "send_telegram"
        ) as send:
            control.handle_update(state, update, Path(tmp) / "dispatch.json")
        sent_text = send.call_args.args[0]
        self.assertIn("إحصائيات قناة نداء اليقظة", sent_text)
        self.assertNotIn("استخدم /research", sent_text)

    def test_arabic_status_and_last_buttons_route_to_runtime_and_release_views(self):
        state = control.default_state()
        runtime = {
            "active": True,
            "scope": "short",
            "kind": "short",
            "topic": "موضوع",
            "stage": "3/6 الصوت ✅",
            "run_url": "https://github.example/run/1",
        }
        release_delivery = {
            "kind": "short",
            "topic": "موضوع سابق",
            "browser_download_url": "https://github.example/releases/download/tag/final.mp4",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"TELEGRAM_CHAT_ID": "123"}, clear=False
        ), mock.patch.object(control, "load_runtime_status", return_value=runtime), mock.patch.object(
            control, "latest_release_delivery", return_value=release_delivery
        ), mock.patch.object(control, "send_telegram") as send:
            for label in ("🟢 حالة الإنتاج", "🎥 آخر إنتاج"):
                update = {
                    "message": {
                        "from": {"id": 123},
                        "chat": {"id": 123},
                        "text": label,
                    }
                }
                control.handle_update(state, update, Path(tmp) / "dispatch.json")
        texts = [call.args[0] for call in send.call_args_list]
        self.assertTrue(any("يوجد إنتاج يعمل الآن" in text for text in texts))
        self.assertTrue(any("آخر إنتاج ناجح" in text for text in texts))


    def test_saved_library_keeps_three_types_and_research_order(self):
        state = control.default_state()
        for idea_id, title in (
            ("l1", "فكرة طويلة أولى"),
            ("l2", "فكرة طويلة ثانية"),
            ("s1", "فكرة شورت"),
            ("p1", "فكرة بودكاست أولى"),
            ("p2", "فكرة بودكاست ثانية"),
            ("p3", "فكرة بودكاست ثالثة"),
        ):
            state["ideas"].append(
                {
                    "idea_id": idea_id,
                    "title": title,
                    "market_evidence": {
                        "sample_count": 1,
                        "distinct_channels": 1,
                        "top_samples": [{"video_id": idea_id, "title": "مصدر"}],
                    },
                    "research_pack": [],
                    "selected": False,
                }
            )
        state["sessions"] = {
            "long-old": {
                "session_id": "long-old",
                "scope": "long",
                "idea_ids": ["l1", "l2"],
                "created_at": "2026-09-24T10:00:00Z",
            },
            "short-new": {
                "session_id": "short-new",
                "scope": "short",
                "idea_ids": ["s1"],
                "created_at": "2026-09-25T10:00:00Z",
            },
            "podcast-newest": {
                "session_id": "podcast-newest",
                "scope": "podcast",
                "idea_ids": ["p1", "p2", "p3"],
                "created_at": "2026-09-25T12:00:00Z",
            },
        }
        with mock.patch.object(control, "_release_library_records", return_value=[]):
            text, keyboard = control._library_menu(state, "saved")
            podcast = control._saved_library_items(state, "podcast", [])
            page_text, page_keyboard = control._saved_library_view(state, "podcast")

        self.assertEqual([row[0]["callback_data"] for row in keyboard[:3]], [
            "library:saved:long",
            "library:saved:short",
            "library:saved:podcast",
        ])
        self.assertEqual(keyboard[-1][0]["callback_data"], "main:home")
        self.assertIn("🎬 طويل — 2", text)
        self.assertIn("⚡ شورت — 1", text)
        self.assertIn("🎙️ بودكاست — 3", text)
        self.assertEqual([item["title"] for item in podcast], [
            "فكرة بودكاست أولى",
            "فكرة بودكاست ثانية",
            "فكرة بودكاست ثالثة",
        ])
        self.assertIn("ترتيب 1 ثم 2 ثم 3", page_text)
        self.assertTrue(page_keyboard[0][0]["text"].startswith("1️⃣"))
        self.assertTrue(page_keyboard[1][0]["text"].startswith("2️⃣"))
        self.assertTrue(page_keyboard[2][0]["text"].startswith("3️⃣"))

    def test_saved_library_uses_only_candidates_actually_shown_in_research(self):
        state = control.default_state()
        state["ideas"] = [
            {"idea_id": "shown", "title": "فكرة ظهرت للمستخدم"},
            {"idea_id": "internal", "title": "فكرة داخلية لم تعرض"},
        ]
        state["sessions"]["s1"] = {
            "session_id": "s1",
            "scope": "long",
            "idea_ids": ["shown"],
            "created_at": "2026-09-25T12:00:00Z",
        }
        items = control._saved_library_items(state, "long", [])
        self.assertEqual([item["idea_id"] for item in items], ["shown"])

    def test_successful_release_moves_topic_out_of_saved_view(self):
        state = control.default_state()
        state["ideas"] = [{"idea_id": "p1", "title": "موضوع بودكاست ناجح"}]
        state["sessions"]["s1"] = {
            "session_id": "s1",
            "scope": "podcast",
            "idea_ids": ["p1"],
            "created_at": "2026-09-25T12:00:00Z",
        }
        used = [{"kind": "podcast", "topic": "موضوع بودكاست ناجح"}]
        self.assertEqual(control._saved_library_items(state, "podcast", used), [])

    def test_release_library_separates_long_short_and_podcast(self):
        payload = [
            {
                "draft": False,
                "tag_name": "clean-v2-final-podcast-1",
                "name": "Clean V2 Podcast — بودكاست ناجح",
                "published_at": "2026-09-25T12:00:00Z",
                "assets": [],
            },
            {
                "draft": False,
                "tag_name": "clean-v2-final-short-1",
                "name": "Clean V2 Short — شورت ناجح",
                "published_at": "2026-09-25T11:00:00Z",
                "assets": [],
            },
            {
                "draft": False,
                "tag_name": "clean-v2-final-film-1",
                "name": "Clean V2 Film — طويل ناجح",
                "published_at": "2026-09-25T10:00:00Z",
                "assets": [],
            },
        ]
        with mock.patch.object(control, "_github_release_json", return_value=payload):
            records = control._release_library_records()
        self.assertEqual([item["kind"] for item in records], ["podcast", "short", "long"])

    def test_saved_pick_does_not_reorder_original_research_library(self):
        state = control.default_state()
        state["ideas"] = [
            {
                "idea_id": "p1",
                "title": "الفكرة الأولى",
                "market_evidence": {
                    "sample_count": 1,
                    "distinct_channels": 1,
                    "top_samples": [{"video_id": "v1", "title": "مصدر"}],
                },
                "research_pack": [],
                "selected": False,
            },
            {
                "idea_id": "p2",
                "title": "الفكرة الثانية",
                "market_evidence": {
                    "sample_count": 1,
                    "distinct_channels": 1,
                    "top_samples": [{"video_id": "v2", "title": "مصدر"}],
                },
                "research_pack": [],
                "selected": False,
            },
        ]
        state["sessions"]["original"] = {
            "session_id": "original",
            "scope": "podcast",
            "idea_ids": ["p1", "p2"],
            "created_at": "2026-09-25T10:00:00Z",
        }
        with mock.patch.object(control, "_release_library_records", return_value=[]):
            control.select_saved_candidate(state, "podcast", "p2")
            items = control._saved_library_items(state, "podcast", [])
        self.assertEqual([item["idea_id"] for item in items], ["p1", "p2"])
        self.assertEqual([item["rank"] for item in items], [1, 2])

    def test_saved_pick_reuses_original_scope_without_dispatch(self):
        state = control.default_state()
        state["ideas"] = [{
            "idea_id": "p1",
            "title": "موضوع بودكاست محفوظ",
            "market_evidence": {
                "sample_count": 1,
                "distinct_channels": 1,
                "top_samples": [{"video_id": "v1", "title": "مصدر"}],
            },
            "research_pack": [],
            "selected": False,
        }]
        with mock.patch.object(control, "_release_library_records", return_value=[]):
            request = control.select_saved_candidate(state, "podcast", "p1")
        self.assertEqual(request["scope"], "podcast")
        self.assertEqual(request["status"], "awaiting_confirmation")
        self.assertIsNone(request["dispatched_at"])

    def test_main_menu_callbacks_are_nested_and_read_only(self):
        state = control.default_state()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"TELEGRAM_CHAT_ID": "123"}, clear=False
        ), mock.patch.object(control, "_release_library_records", return_value=[]), mock.patch.object(
            control, "send_telegram"
        ) as send:
            dispatch = Path(tmp) / "dispatch.json"
            for callback_data in ("main:home", "main:research", "main:saved", "main:used"):
                update = {
                    "callback_query": {
                        "from": {"id": 123},
                        "message": {"chat": {"id": 123}},
                        "data": callback_data,
                    }
                }
                control.handle_update(state, update, dispatch)
                self.assertFalse(dispatch.exists())
        sent = [(call.args[0], call.args[1] if len(call.args) > 1 else None) for call in send.call_args_list]
        self.assertTrue(any("🏠 الرئيسية" in text for text, _ in sent))
        self.assertTrue(any("اختر نوع المحتوى" in text for text, _ in sent))
        self.assertTrue(any("📚 المحفوظات" in text for text, _ in sent))
        self.assertTrue(any("✅ المستعملة" in text for text, _ in sent))

    def test_saved_and_used_commands_never_create_dispatch_file(self):
        state = control.default_state()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"TELEGRAM_CHAT_ID": "123"}, clear=False
        ), mock.patch.object(control, "_release_library_records", return_value=[]), mock.patch.object(
            control, "send_telegram"
        ) as send:
            dispatch = Path(tmp) / "dispatch.json"
            for command in ("/saved", "/used"):
                update = {"message": {"from": {"id": 123}, "chat": {"id": 123}, "text": command}}
                control.handle_update(state, update, dispatch)
                self.assertFalse(dispatch.exists())
        self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
