import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import scene_review as sr
from clean_v2.simple_mode import SIMPLE_MODE_ENV
from scripts import telegram_clean_v2_control as control
from scripts import telegram_scene_review as tsr


def _pack(n_beats=3, per_beat=4):
    return {
        "review_id": "abcd1234",
        "beats": [
            {
                "id": f"b{i}", "index": i, "role": "body", "text_ar": f"نص {i}", "sheet": f"b{i}.jpg",
                "candidates": [
                    {"n": n, "provider": "pexels", "asset_id": f"{i}{n}",
                     "download_url": f"https://videos.pexels.com/{i}{n}.mp4", "source_url": "u", "creator": "c"}
                    for n in range(1, per_beat + 1)
                ],
            }
            for i in range(1, n_beats + 1)
        ],
    }


def _state(scope="short", status="collect"):
    request = {
        "request_id": "req-1", "scope": scope, "status": "dispatched",
        "request_sha256": "sha", "scene_review": {"status": status},
    }
    return {"requests": {"req-1": request}}, request


class ParseReplyTests(unittest.TestCase):
    beats = _pack()["beats"]

    def test_picks_background_and_keep(self):
        out = tsr.parse_reply("مشاهد 2 0 -", self.beats)
        self.assertEqual(out["b1"]["candidate"]["asset_id"], "12")
        self.assertEqual(out["b2"], {"mode": "background"})
        self.assertNotIn("b3", out)

    def test_arabic_digits_commas_and_compact_form(self):
        self.assertEqual(tsr.parse_reply("مشاهد ٢، ٠، -", self.beats), tsr.parse_reply("مشاهد 2 0 -", self.beats))
        self.assertEqual(tsr.parse_reply("مشاهد 20-", self.beats), tsr.parse_reply("مشاهد 2 0 -", self.beats))

    def test_typos_never_pass(self):
        for bad in ("مشاهد 1 1", "مشاهد 1 1 1 1", "مشاهد 9 1 1", "مشاهد 5 1 1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tsr.parse_reply(bad, self.beats)  # 5 > 4 candidates; 9 > 6

    def test_reply_detection_is_narrow(self):
        self.assertTrue(tsr.is_reply_text("مشاهد 1 2 0 -"))
        for other in ("تأكيد الإنتاج", "مشاهد جميلة", "5", "-", "/status", "ok 123"):
            self.assertFalse(tsr.is_reply_text(other))

    def test_bare_digits_are_accepted_like_the_owner_typed_them(self):
        # Real case (Run on 2026-10-14): the owner sent 5354400 without the word.
        for ok in ("5354400", "٥٣٥٤٤٠٠", "1 2 3", "53-4400"):
            self.assertTrue(tsr.is_reply_text(ok), ok)
        beats = _pack(7, 6)["beats"]
        out = tsr.parse_reply("5354400", beats)
        self.assertEqual([out[f"b{i}"]["candidate"]["asset_id"] for i in range(1, 6)], ["15", "23", "35", "44", "54"])
        self.assertEqual(out["b6"], {"mode": "background"})
        self.assertEqual(out["b7"], {"mode": "background"})


class StateMachineTests(unittest.TestCase):
    def test_record_requires_collecting_short_and_matching_hash(self):
        state, _ = _state(status="collect")
        tsr.record_review(state, "req-1", "sha", _pack())
        self.assertEqual(state["requests"]["req-1"]["scene_review"]["status"], "awaiting")
        with self.assertRaises(RuntimeError):
            tsr.record_review(state, "req-1", "WRONG", _pack())
        state2, _ = _state(status="selected")
        with self.assertRaises(RuntimeError):
            tsr.record_review(state2, "req-1", "sha", _pack())

    def test_choice_is_consumed_exactly_once(self):
        state, _ = _state()
        tsr.record_review(state, "req-1", "sha", _pack())
        request, choices, auto = tsr.prepare_choice(state, text="مشاهد 1 2 3")
        tsr.commit_choice(request, choices, auto)
        for kwargs in ({"text": "مشاهد 1 1 1"}, {"auto_review_id": "abcd1234"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LookupError):
                tsr.prepare_choice(state, **kwargs)
        self.assertEqual(request["scene_review"]["status"], "selected")

    def test_old_auto_button_cannot_act_on_a_new_review(self):
        state, _ = _state()
        tsr.record_review(state, "req-1", "sha", _pack())
        with self.assertRaises(LookupError):
            tsr.prepare_choice(state, auto_review_id="oldold00")

    def test_export_only_when_selected(self):
        state, request = _state()
        tsr.record_review(state, "req-1", "sha", _pack())
        with tempfile.TemporaryDirectory() as t:
            out = Path(t) / "c.json"
            self.assertFalse(tsr.export_choices(state, "req-1", out))
            r, choices, auto = tsr.prepare_choice(state, text="مشاهد 1 - 0")
            tsr.commit_choice(r, choices, auto)
            self.assertTrue(tsr.export_choices(state, "req-1", out))
            loaded = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(loaded["review_id"], "abcd1234")
            self.assertFalse(loaded["auto_all"])
            self.assertEqual(loaded["choices"]["b3"], {"mode": "background"})


class ControlIntegrationTests(unittest.TestCase):
    def _ready_state(self):
        state, _ = _state()
        tsr.record_review(state, "req-1", "sha", _pack())
        return state

    def test_reply_dispatches_phase_two_once(self):
        state = self._ready_state()
        sent = []
        with tempfile.TemporaryDirectory() as t, \
                mock.patch.object(control, "send_telegram", lambda text, kb=None: sent.append(text)), \
                mock.patch.object(control, "stage_voice_resume_dispatch", return_value={"available": True}) as stage:
            control._apply_scene_review(state, Path(t) / "d.json", text="مشاهد 1 2 0")
            control._apply_scene_review(state, Path(t) / "d.json", text="مشاهد 3 3 3")
            control._apply_scene_review(state, Path(t) / "d.json", auto_review_id="abcd1234")
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(state["requests"]["req-1"]["scene_review"]["status"], "selected")
        self.assertIn("اعتُمد", sent[0])
        self.assertTrue(all("لا توجد مراجعة" in m for m in sent[1:]))

    def test_unavailable_voice_does_not_consume_the_review(self):
        state = self._ready_state()
        sent = []
        with tempfile.TemporaryDirectory() as t, \
                mock.patch.object(control, "send_telegram", lambda text, kb=None: sent.append(text)), \
                mock.patch.object(control, "stage_voice_resume_dispatch", return_value={"available": False, "reason": "x"}):
            control._apply_scene_review(state, Path(t) / "d.json", text="مشاهد 1 2 0")
        self.assertEqual(state["requests"]["req-1"]["scene_review"]["status"], "awaiting")

    def test_bare_digits_act_only_while_a_review_is_waiting(self):
        state, _ = _state()
        sent = []
        with tempfile.TemporaryDirectory() as t, \
                mock.patch.object(control, "send_telegram", lambda text, kb=None: sent.append(text)), \
                mock.patch.object(control, "stage_voice_resume_dispatch", return_value={"available": True}) as stage:
            update = lambda text: {"message": {"text": text, "from": {"id": 1}, "chat": {"id": 1}}}
            with mock.patch.object(control, "authorized", return_value=True):
                control.handle_update(state, update("5354400"), Path(t) / "d.json")  # nothing waiting
                stage.assert_not_called()
                self.assertIn("استخدم /research", sent[-1])
                tsr.record_review(state, "req-1", "sha", _pack(7, 6))
                control.handle_update(state, update("5354400"), Path(t) / "d.json")
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(state["requests"]["req-1"]["scene_review"]["status"], "selected")

    def test_typo_dispatches_nothing(self):
        state = self._ready_state()
        sent = []
        with tempfile.TemporaryDirectory() as t, \
                mock.patch.object(control, "send_telegram", lambda text, kb=None: sent.append(text)), \
                mock.patch.object(control, "stage_voice_resume_dispatch") as stage:
            control._apply_scene_review(state, Path(t) / "d.json", text="مشاهد 1 2")
        stage.assert_not_called()
        self.assertEqual(state["requests"]["req-1"]["scene_review"]["status"], "awaiting")
        self.assertIn("لم أفهم", sent[0])

    def test_only_shorts_enter_review_and_hash_is_untouched(self):
        for scope, expected in (("short", True), ("long", False), ("podcast", False)):
            with self.subTest(scope=scope):
                state = control.default_state()
                request = {
                    "request_id": "r", "scope": scope, "status": "awaiting_confirmation", "source": "s",
                    "approved_by_user": True, "approved_topic": "t", "research_pack": [], "idea_id": "i",
                    "schema_version": 1, "selected_at": "x",
                }
                request["request_sha256"] = control._request_hash(request)
                state["requests"]["r"] = request
                state["current_request_id"] = "r"
                with tempfile.TemporaryDirectory() as t:
                    control._stage_confirmation_dispatch(state, Path(t) / "d.json")
                self.assertEqual(bool(request.get("scene_review")), expected)
                self.assertEqual(request["request_sha256"], control._request_hash(request))


def _vp():
    from scripts import test_clean_v2_visual_pacing as vp
    return vp



class SemanticShortlistTests(unittest.TestCase):
    def test_generic_filler_and_unknown_metadata_are_not_review_candidates(self):
        from clean_v2.media import StockVisualSource
        source = StockVisualSource()
        source._review_taken = set()
        query = "person declines invitation with raised palm"
        bad = [
            {"provider": "pexels", "asset_id": "coffee",
             "download_url": "https://example.invalid/a.mp4",
             "retrieval_description": "coffee cup poured on cafe table"},
            {"provider": "pexels", "asset_id": "slug",
             "download_url": "https://example.invalid/b.mp4",
             "retrieval_description": ""},
        ]
        with mock.patch.object(source, "_pexels_recovery_pool", return_value=bad), \
                mock.patch.object(source, "_pixabay_recovery_pool", return_value=[]), \
                mock.patch.object(source, "_coverr_recovery_pool", return_value=[]):
            result = source._review_entry(
                {"id": "b1", "role": "body", "shot_intent": query}, query, portrait=True)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["search_queries"]), 1)

    def test_relevant_action_survives_without_filling_six_slots(self):
        from clean_v2.media import StockVisualSource
        source = StockVisualSource()
        source._review_taken = set()
        query = "person declines invitation with raised palm"
        good = {"provider": "pexels", "asset_id": "no",
                "download_url": "https://example.invalid/no.mp4",
                "retrieval_description": "person declines invitation raising palm to visitor"}
        with mock.patch.object(source, "_pexels_recovery_pool", return_value=[good]), \
                mock.patch.object(source, "_pixabay_recovery_pool", return_value=[]), \
                mock.patch.object(source, "_coverr_recovery_pool", return_value=[]):
            result = source._review_entry(
                {"id": "b1", "role": "body", "shot_intent": query}, query, portrait=True)
        self.assertEqual([r["asset_id"] for r in result["candidates"]], ["no"])

    def test_generic_notebook_prompt_cannot_supply_review_filler(self):
        from clean_v2.media import StockVisualSource
        source = StockVisualSource()
        source._review_taken = set()
        with mock.patch.object(source, "_pexels_recovery_pool") as p:
            result = source._review_entry(
                {"id": "b1", "role": "body", "shot_intent": "person writing at desk"},
                "person writing at desk", portrait=True)
        self.assertEqual(result["candidates"], [])
        p.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class AcquireReviewTests(unittest.TestCase):
    def _source(self):
        from clean_v2 import media as media_module
        vp = _vp()
        helper = vp.StockVisualSourceAcquireBeatTests("_story")
        beats = [helper._beat(f"b{i}", "s1", f"closed notebook scene number {i} on desk") for i in (1, 2, 3)]
        plan = vp._pacing_plan("s1")
        plan["visual_story"] = helper._story(*beats)
        return media_module, media_module.StockVisualSource(), plan, vp

    def _acquire(self, env, *, choices=None, pools=None):
        media_module, source, plan, vp = self._source()
        counter = {"n": 0}

        def fake_pexels(query, *, portrait):
            counter["n"] += 1
            return vp._candidate("pexels", f"auto{counter['n']}")

        def fake_download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"V" * 4096)

        def pool(query, *, portrait, limit):
            return [dict(vp._candidate("pexels", f"r{abs(hash(query)) % 999}-{i}"), retrieval_description=query) for i in range(limit)]

        with tempfile.TemporaryDirectory() as root:
            env = dict(env)
            if choices is not None:
                choice_file = Path(root) / "choices.json"
                choice_file.write_text(json.dumps(choices), encoding="utf-8")
                env[sr.CHOICES_ENV] = str(choice_file)
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(source, "_pexels", side_effect=fake_pexels), \
                    mock.patch.object(source, "_pixabay", return_value=None), \
                    mock.patch.object(source, "_pexels_recovery_pool", side_effect=pool), \
                    mock.patch.object(source, "_pixabay_recovery_pool", return_value=[]), \
                    mock.patch.object(source, "_coverr_recovery_pool", return_value=[]), \
                    mock.patch.object(media_module, "_download_media", side_effect=fake_download), \
                    mock.patch.object(media_module, "_short_visual_color_compatible", return_value=(False, "dark")):
                result = source.acquire(plan, Path(root), "short", 5)
            return result, source

    def test_collect_mode_renders_nothing_and_shortlists_per_beat(self):
        (clips, rights), source = self._acquire({SIMPLE_MODE_ENV: "1", sr.COLLECT_ENV: "collect"})
        self.assertEqual((clips, rights), ([], []))
        self.assertEqual([b["id"] for b in source.review_pack], ["b1", "b2", "b3"])
        shown = [(c["provider"], c["asset_id"]) for b in source.review_pack for c in b["candidates"]]
        self.assertEqual(len(shown), len(set(shown)), "a clip must never be offered twice")
        self.assertTrue(all(b["candidates"] for b in source.review_pack))

    def _first_answer_fixture(self, root):
        # Real Run 135 shape: topic speech starts inside s1 (15.7 < s1 end 19.94).
        from clean_v2.identity_sequence import PRAYER_SENTENCE
        (root / "timeline-first.json").write_text(json.dumps({
            "identity_events": [{"kind": "hook", "start": 0.0, "end": 5.36}, {"kind": "topic", "start": 15.7, "end": 54.0}],
            "section_events": [{"section_id": "s1", "start": 0.0, "end": 19.94}],
        }), encoding="utf-8")
        (root / "script.json").write_text(json.dumps({
            "sections": [{"id": "s1", "narration": f"افتتاح. {PRAYER_SENTENCE} الجواب الأول يبدأ هنا."}],
        }, ensure_ascii=False), encoding="utf-8")

    def _acquire_with_timeline(self, env):
        media_module, source, plan, vp = self._source()
        for beat in plan["visual_story"]["beats"]:
            beat["role"] = "hook"  # as planned in Runs 127/131/135: every s1 beat is a hook beat

        def pool(query, *, portrait, limit):
            return [dict(vp._candidate("pexels", f"r{abs(hash(query)) % 999}-{i}"), retrieval_description=query) for i in range(limit)]

        def fake_download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"V" * 4096)

        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            self._first_answer_fixture(root)
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(source, "_pexels", side_effect=lambda q, *, portrait: vp._candidate("pexels", f"a{abs(hash(q)) % 999}")), \
                    mock.patch.object(source, "_pixabay", return_value=None), \
                    mock.patch.object(source, "_pexels_recovery_pool", side_effect=pool), \
                    mock.patch.object(source, "_pixabay_recovery_pool", return_value=[]), \
                    mock.patch.object(source, "_coverr_recovery_pool", return_value=[]), \
                    mock.patch.object(media_module, "_download_media", side_effect=fake_download), \
                    mock.patch.object(media_module, "_short_visual_color_compatible", return_value=(True, "ok")):
                result = source.acquire(plan, root / "visuals", "short", 5)
            return result, source

    def test_short_first_answer_gets_its_own_background_after_the_hook_beats(self):
        (_clips, rights), _ = self._acquire_with_timeline({SIMPLE_MODE_ENV: "1"})
        ids = [r["beat_id"] for r in rights]
        self.assertEqual(ids, ["b1", "b2", "b3", "s1-topic"])
        self.assertEqual(rights[-1]["provider"], "generated_local")
        self.assertEqual(rights[-1]["role"], "body")  # a non-hook visual covers the topic window

    def test_first_answer_window_is_offered_for_review_without_changing_auto_behavior(self):
        (clips, rights), source = self._acquire_with_timeline({SIMPLE_MODE_ENV: "1", sr.COLLECT_ENV: "collect"})
        self.assertEqual((clips, rights), ([], []))
        self.assertEqual([b["id"] for b in source.review_pack], ["b1", "b2", "b3", "s1-topic"])
        answer = source.review_pack[-1]
        self.assertEqual(answer["role"], "body")
        self.assertIn("الجواب الأول", answer["text_ar"])
        # Review must preserve the first spoken-answer slot even when strict
        # semantic preflight correctly finds no credible stock candidates.
        self.assertIsInstance(answer["candidates"], list)
        self.assertIsInstance(answer["search_queries"], list)
        self.assertNotIn("local symbolic first answer", answer["search_queries"])

    def test_review_entry_carries_the_spoken_text(self):
        _, source, _plan, _vp_mod = self._source()
        source.review_pack, source._review_taken = [], set()
        with mock.patch.object(source, "_pexels_recovery_pool", return_value=[]), \
                mock.patch.object(source, "_pixabay_recovery_pool", return_value=[]), \
                mock.patch.object(source, "_coverr_recovery_pool", return_value=[]):
            entry = source._review_entry(
                {"id": "b1", "role": "hook", "writer_anchor_ar": "لماذا تتجمد أحياناً", "shot_intent": "x"},
                "person at shelf", portrait=True,
            )
        self.assertEqual(entry["text_ar"], "لماذا تتجمد أحياناً")
        self.assertEqual(entry["candidates"], [])

    def test_review_alternate_query_appears_in_first_six_candidates(self):
        from clean_v2 import media as media_module
        source = media_module.StockVisualSource()
        source._review_taken = set()
        queries_seen = []

        def pool(query, *, portrait, limit):
            queries_seen.append(query)
            return [
                {"provider": "pexels", "asset_id": f"{query}-{i}",
                 "download_url": f"https://media.invalid/{i}.mp4", "query": query,
                 "retrieval_description": query}
                for i in range(limit)
            ]

        beat = {"id": "b1", "role": "body", "writer_anchor_ar": "النص",
                "shot_intent": "notebook being closed",
                "stock_query_alt_en": "alarm clock showing time"}
        with mock.patch.object(source, "_pexels_recovery_pool", side_effect=pool), \
                mock.patch.object(source, "_pixabay_recovery_pool", return_value=[]), \
                mock.patch.object(source, "_coverr_recovery_pool", return_value=[]):
            entry = source._review_entry(beat, "notebook being closed", portrait=True)
        self.assertEqual(len(entry["search_queries"]), 2)
        self.assertEqual(len(entry["candidates"]), 8)
        first_six = entry["candidates"][:6]
        self.assertEqual(len({c["query"] for c in first_six}), 2)
        self.assertEqual(len({c["asset_id"] for c in first_six}), 6)
        self.assertEqual(len(queries_seen), 2)

    def test_owner_choices_are_applied_in_beat_order(self):
        vp = _vp()
        picked = {k: vp._candidate("pexels", "chosen")[k] for k in ("provider", "asset_id", "download_url")}
        choices = {
            "schema_version": 1, "review_id": "x", "auto_all": False,
            "choices": {"b1": {"mode": "candidate", "candidate": picked}, "b2": {"mode": "background"}},
        }
        (clips, rights), _ = self._acquire({SIMPLE_MODE_ENV: "1"}, choices=choices)
        self.assertEqual([r["beat_id"] for r in rights], ["b1", "b2", "b3"])
        self.assertEqual(rights[0]["asset_id"], "chosen")
        self.assertEqual(rights[0]["human_choice"], "candidate")  # owner pick bypasses the colour heuristic
        self.assertEqual(rights[1]["human_choice"], "background")
        self.assertEqual(rights[1]["provider"], "generated_local")
        self.assertEqual(rights[2]["human_choice"], "auto")
        self.assertEqual(len(clips), 3)

    def test_auto_all_marks_every_scene_unreviewed_by_human_choice(self):
        choices = {"schema_version": 1, "review_id": "x", "auto_all": True, "choices": {}}
        (_clips, rights), _ = self._acquire({SIMPLE_MODE_ENV: "1"}, choices=choices)
        self.assertEqual([r["human_choice"] for r in rights], ["auto"] * 3)

    def test_without_simple_mode_or_files_nothing_changes(self):
        (_clips, rights), _ = self._acquire({}, choices=None)
        self.assertTrue(all("human_choice" not in r for r in rights))


class SceneReportCoverageTests(unittest.TestCase):
    def test_auxiliary_choices_and_failed_picks_are_counted(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            (output / "rights-manifest.json").write_text(json.dumps({
                "assets": [
                    {"human_choice": "candidate"},
                    {"human_choice": "background", "story_beat_auxiliary": True},
                    {"human_choice": "candidate_failed", "story_beat_auxiliary": True},
                    {"human_choice": "auto", "story_beat_auxiliary": True},
                    {"provider": "generated_local"},
                ]
            }), encoding="utf-8")
            report = sr.report_block(output)
        self.assertEqual(report["human_chosen_clips"], 1)
        self.assertEqual(report["human_background"], 1)
        self.assertEqual(report["human_choice_failed"], 1)
        self.assertEqual(report["kept_automatic"], 1)
        self.assertEqual(report["auxiliary_slots_counted"], 3)
        self.assertEqual(report["unmarked_visual_slots"], 1)
        self.assertEqual(report["total_visual_slots"], 5)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
class CollectPackTests(unittest.TestCase):
    def test_collect_writes_numbered_contact_sheets_and_pack(self):
        import subprocess
        from clean_v2 import media as media_module

        class Source:
            review_pack = []

            def acquire(self, *_a, **_k):
                self.review_pack = [{
                    "id": "b1", "role": "hook", "text_ar": "نص", "shot_intent": "x",
                    "candidates": [
                        {"provider": "pexels", "asset_id": str(i), "download_url": f"https://x/{i}.mp4"}
                        for i in range(8)
                    ] + [{"provider": "pexels", "asset_id": "bad", "download_url": "https://x/bad.mp4"}],
                }, {"id": "b2", "role": "body", "text_ar": "ثاني", "shot_intent": "y", "candidates": []}]
                return [], []

        with tempfile.TemporaryDirectory() as t:
            sample = Path(t) / "sample.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=10:duration=2",
                 "-pix_fmt", "yuv420p", str(sample)], check=True)

            def fake_download(url, destination):
                if url.endswith("bad.mp4"):
                    raise RuntimeError("boom")
                Path(destination).write_bytes(sample.read_bytes())

            out = Path(t) / "out"
            with mock.patch.object(media_module, "_download_media", side_effect=fake_download):
                pack = sr.collect(Source(), {}, out / "visuals", "short", 5, out)
            first, second = pack["beats"]
            self.assertEqual([c["n"] for c in first["candidates"]], [1, 2, 3, 4, 5, 6])
            self.assertTrue((out / "scene-review" / "b1.jpg").stat().st_size > 5000)
            self.assertEqual(second["candidates"], [])
            self.assertEqual(second["sheet"], "")
            saved = json.loads((out / sr.REVIEW_FILE).read_text(encoding="utf-8"))
            self.assertEqual(saved["review_id"], pack["review_id"])
            self.assertFalse(list((out / "scene-review").glob("*-c*.jpg")), "per-candidate temp frames must be removed")
            state, _ = _state()
            tsr.record_review(state, "req-1", "sha", saved)  # phase 1 output is accepted by phase 1.5


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class PipelineStopsCleanlyTests(unittest.TestCase):
    def test_phase_one_stops_after_the_saved_voice_without_a_video(self):
        # The hook is Short-only in production. The pipeline fixture is a film brief, so
        # the supported format is switched for this test to exercise the real stop path.
        from clean_v2.contracts import compute_brief_sha256
        from clean_v2.pipeline import CleanV2Pipeline
        from scripts import test_clean_v2 as base

        class Visuals(base._FakeVisuals):
            def acquire(self, *_a, **_k):
                raise AssertionError("phase 1 must not render visuals")

        def fake_collect(source, plan, visuals_dir, fmt, max_visuals, output_dir):
            pack = _pack(2, 3)
            (Path(output_dir) / sr.REVIEW_FILE).write_text(json.dumps(pack), encoding="utf-8")
            return pack

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict(os.environ, {sr.COLLECT_ENV: "collect"}), \
                mock.patch.object(sr, "SUPPORTED_FORMAT", "film"), \
                mock.patch.object(sr, "collect", fake_collect):
            root = Path(temporary)
            brief = base._brief()
            brief_path = root / "approved-brief.json"
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"
            pipeline = CleanV2Pipeline(
                router=base._FakeRouter(), voice_synthesizer=base._FakeVoice(), visual_source=Visuals(),
                visual_qa=base._passing_visual_qa, cinematic_layer=base._passing_cinematic_layer,
                final_master_qc=base._passing_final_master_qc, text_audit=base._passing_text_audit,
                audio_mastering=base._passing_audio_mastering, narrative_identity=base._passing_narrative_identity,
            )
            result = pipeline.run(
                brief_path=brief_path, approved_sha256=compute_brief_sha256(brief),
                output_dir=output, engine_sha="a" * 40, runner_sha="b" * 40, max_visuals=2,
            )
            self.assertEqual(result["status"], sr.PENDING_STATUS)
            manifest = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], sr.PENDING_STATUS)
            self.assertNotIn("final_file", manifest)
            self.assertFalse((output / "final.mp4").exists())
            checkpoint = json.loads((output / "resume-checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["completed_stage"], "voice")  # phase 2 resumes from here
            self.assertTrue((output / sr.REVIEW_FILE).is_file())


class ReportTests(unittest.TestCase):
    def _report(self, marks):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "rights-manifest.json").write_text(
                json.dumps({"assets": [{"human_choice": m} for m in marks]}), encoding="utf-8")
            return sr.report_block(Path(t))

    def test_report_is_honest_about_review(self):
        self.assertEqual(self._report(["", ""]), {"mode": "not_applicable"})
        human = self._report(["candidate", "background", "auto"])
        self.assertEqual(human["mode"], "human_reviewed")
        self.assertEqual((human["human_chosen_clips"], human["human_background"], human["kept_automatic"]), (1, 1, 1))
        self.assertEqual(self._report(["auto", "auto"])["mode"], "auto_all_by_owner_decision")


class ModeFlagTests(unittest.TestCase):
    def test_collect_and_choices_only_for_short(self):
        with mock.patch.dict(os.environ, {sr.COLLECT_ENV: "collect"}):
            self.assertTrue(sr.collect_mode("short"))
            self.assertFalse(sr.collect_mode("film"))
            self.assertFalse(sr.collect_mode("podcast"))
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(sr.COLLECT_ENV, None)
            self.assertFalse(sr.collect_mode("short"))
            os.environ.pop(sr.CHOICES_ENV, None)
            self.assertIsNone(sr.load_choices("short"))


if __name__ == "__main__":
    unittest.main()
