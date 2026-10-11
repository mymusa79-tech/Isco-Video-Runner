import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import pipeline as pl
from clean_v2.simple_mode import (
    SIMPLE_MODE_ENV,
    advisory_tone_report,
    is_simple_mode,
    simple_opening_director,
    simple_visual_qa,
    tone_audit_state,
    tone_block_is_objective,
    write_simple_report,
)

SUBJECTIVE = {
    "status": "block",
    "narrative_format_flags": ["content_depth:s3 practical_action_generic: x"],
    "unverified_religious_quote_flags": [],
    "cultural_dignity_flags": [],
}


class SimpleModeUnitTests(unittest.TestCase):
    def test_flag_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(SIMPLE_MODE_ENV, None)
            self.assertFalse(is_simple_mode())

    def test_objective_tone_flags_still_block(self):
        self.assertFalse(tone_block_is_objective(SUBJECTIVE))
        self.assertTrue(tone_block_is_objective({**SUBJECTIVE, "unverified_religious_quote_flags": ["q"]}))
        self.assertTrue(tone_block_is_objective({**SUBJECTIVE, "cultural_dignity_flags": ["d"]}))
        out = advisory_tone_report(SUBJECTIVE)
        self.assertEqual(out["status"], "pass")
        self.assertEqual(out["downgraded_from"], "block")

    def _audits(self, tone_report):
        def fake_fact(**_):
            return {"status": "pass"}

        def fake_tone(**_):
            raise pl.CleanV2ToneContentBlock(tone_report)

        return fake_fact, fake_tone

    def test_text_audit_default_path_still_blocks_subjective_tone(self):
        fact, tone = self._audits(SUBJECTIVE)
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {}), \
                mock.patch.object(pl, "_run_legacy_factuality_audit", fact), \
                mock.patch.object(pl, "_run_legacy_tone_naturalness_audit", tone):
            os.environ.pop(SIMPLE_MODE_ENV, None)
            with self.assertRaises(pl.CleanV2ToneContentBlock):
                pl._run_text_audits(output_dir=Path(t), brief={}, plan={}, script={})

    def test_text_audit_simple_mode_downgrades_subjective_but_not_objective(self):
        fact, tone = self._audits(SUBJECTIVE)
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}), \
                mock.patch.object(pl, "_run_legacy_factuality_audit", fact), \
                mock.patch.object(pl, "_run_legacy_tone_naturalness_audit", tone):
            out = pl._run_text_audits(output_dir=Path(t), brief={}, plan={}, script={})
            self.assertEqual(out["status"], "pass")
            saved = json.loads((Path(t) / "tone-naturalness-audit.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["simple_mode_advisory"])
        fact, tone = self._audits({**SUBJECTIVE, "unverified_religious_quote_flags": ["q"]})
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}), \
                mock.patch.object(pl, "_run_legacy_factuality_audit", fact), \
                mock.patch.object(pl, "_run_legacy_tone_naturalness_audit", tone):
            with self.assertRaises(pl.CleanV2ToneContentBlock):
                pl._run_text_audits(output_dir=Path(t), brief={}, plan={}, script={})

    def test_factuality_block_still_blocks_in_simple_mode(self):
        def fact(**_):
            raise pl.CleanV2FactualityContentBlock({"status": "block"})

        _, tone = self._audits(SUBJECTIVE)
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}), \
                mock.patch.object(pl, "_run_legacy_factuality_audit", fact), \
                mock.patch.object(pl, "_run_legacy_tone_naturalness_audit", tone):
            with self.assertRaises(pl.CleanV2FactualityContentBlock):
                pl._run_text_audits(output_dir=Path(t), brief={}, plan={}, script={})

    def test_hook_tension_validator_relaxed_only_in_simple_mode(self):
        from clean_v2.short_format import ShortFormatError, validate_short_hook_immediate_tension

        hook = "سعر الخبز في مدينتنا ارتفع هذا الشهر"
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(SIMPLE_MODE_ENV, None)
            with self.assertRaises(ShortFormatError):
                validate_short_hook_immediate_tension(hook)
        with mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
            self.assertEqual(validate_short_hook_immediate_tension(hook), "simple_mode_tension_unverified")
            with self.assertRaises(ShortFormatError):
                validate_short_hook_immediate_tension("في حياتنا اليومية نعتمد على الآخرين كثيراً")

    def test_visual_qa_report_never_claims_a_match(self):
        with tempfile.TemporaryDirectory() as t:
            rights = [
                {"provider": "pexels", "asset_id": "1"},
                {"provider": "generated_local", "asset_id": "bg"},
            ]
            rep = simple_visual_qa(output_dir=Path(t), plan={}, script={}, rights=rights, fmt="short")
            self.assertEqual(rep["status"], "pass")
            self.assertEqual(rep["semantic_review"], "not_performed")
            self.assertEqual(rep["fallback_backgrounds"], 1)
            final = write_simple_report(Path(t), fmt="short")
            self.assertEqual(final["semantic_visual_review"], "not_performed")
            self.assertEqual(final["publish_decision"], "owner")
            self.assertNotIn("matched", json.dumps(final).lower())
            self.assertEqual(simple_opening_director()["status"], "skipped_simple_mode")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class SimpleModeBeatOrderTests(unittest.TestCase):
    """A failed beat keeps its own slot; nothing is moved, duplicated or lost."""

    def _run(self, *, fmt, failing, simple, section_ids=("s1", "s1", "s1")):
        from clean_v2 import media as media_module
        from scripts import test_clean_v2_visual_pacing as vp

        helper = vp.StockVisualSourceAcquireBeatTests("_story")
        beats = [
            helper._beat(f"b{i}", sid, f"closed notebook scene number {i} on desk")
            for i, sid in enumerate(section_ids, start=1)
        ]
        source = media_module.StockVisualSource()
        counter = {"n": 0}

        def fake_pexels(query, *, portrait):
            del portrait
            if any(
                f"number {b[1:]} " in f"{query} " or f"action {b} " in f"{query} " for b in failing
            ):
                return None
            counter["n"] += 1
            return vp._candidate("pexels", f"p{counter['n']}")

        def fake_download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"V" * 4096)

        plan = vp._pacing_plan(*dict.fromkeys(section_ids))
        plan["visual_story"] = helper._story(*beats)
        env = {SIMPLE_MODE_ENV: "1"} if simple else {}
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, env), mock.patch.object(
            source, "_pexels", side_effect=fake_pexels
        ), mock.patch.object(source, "_pixabay", return_value=None), mock.patch.object(
            media_module, "_download_media", side_effect=fake_download
        ), mock.patch.object(
            media_module, "_short_visual_color_compatible", return_value=(True, "ok")
        ):
            if not simple:
                os.environ.pop(SIMPLE_MODE_ENV, None)
            return source.acquire(
                plan, Path(root), fmt, 5,
                section_estimated_seconds={sid: 30.0 for sid in dict.fromkeys(section_ids)},
            )

    def test_middle_beat_failure_keeps_order_in_every_format(self):
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                clips, rights = self._run(fmt=fmt, failing={"b2"}, simple=True)
                self.assertEqual([r["beat_id"] for r in rights], ["b1", "b2", "b3"])
                self.assertEqual(len(clips), 3)
                self.assertEqual(len({r["beat_id"] for r in rights}), 3)
                self.assertEqual(
                    [r["provider"] == "generated_local" for r in rights], [False, True, False]
                )
                self.assertEqual(rights[1]["section_id"], "s1")
                self.assertTrue(rights[1]["local_file"].startswith("visual-fallback-b2"))

    def test_first_and_last_failures_also_keep_slots(self):
        _clips, rights = self._run(fmt="film", failing={"b1", "b3"}, simple=True)
        self.assertEqual([r["beat_id"] for r in rights], ["b1", "b2", "b3"])
        self.assertEqual(
            [r["provider"] == "generated_local" for r in rights], [True, False, True]
        )

    def test_standard_path_never_adds_backgrounds(self):
        _clips, rights = self._run(fmt="film", failing={"b2"}, simple=False)
        self.assertTrue(all(r["provider"] != "generated_local" for r in rights))
        self.assertNotIn("b2", [r["beat_id"] for r in rights])


class SimpleModeToneOutageTests(unittest.TestCase):
    def _run(self, exc, *, simple=True, fact_block=False):
        def fact(**_):
            if fact_block:
                raise pl.CleanV2FactualityContentBlock({"status": "block"})
            return {"status": "pass"}

        def tone(**_):
            raise exc

        env = {SIMPLE_MODE_ENV: "1"} if simple else {}
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, env), \
                mock.patch.object(pl, "_run_legacy_factuality_audit", fact), \
                mock.patch.object(pl, "_run_legacy_tone_naturalness_audit", tone):
            if not simple:
                os.environ.pop(SIMPLE_MODE_ENV, None)
            out = pl._run_text_audits(output_dir=Path(t), brief={}, plan={}, script={})
            saved = json.loads((Path(t) / "tone-naturalness-audit.json").read_text(encoding="utf-8"))
            rep = write_simple_report(Path(t), fmt="short") if saved.get("audit_availability") else None
            return out, saved, tone_audit_state(Path(t)), rep

    def test_known_provider_outages_do_not_stop_simple_mode_and_are_not_a_pass(self):
        from clean_v2.mistral_executor import MistralExecutorWireFailure
        from clean_v2.providers import ProviderWireFailure

        for exc in (
            ProviderWireFailure("http_429", http_status=429),
            ProviderWireFailure("http_503", http_status=503),
            ProviderWireFailure("timeout"),
            MistralExecutorWireFailure("http_429"),
            TimeoutError("read timed out"),
        ):
            with self.subTest(exc=repr(exc)):
                out, saved, state, rep = self._run(exc)
                self.assertEqual(saved["audit_availability"], "unavailable")
                self.assertFalse(saved["tone_audit_reviewed"])
                self.assertEqual(state, "unavailable")
                self.assertEqual(rep["tone_audit"], "unavailable")
                self.assertIn("NOT passed", rep["statement"])

    def test_unknown_exceptions_and_client_errors_still_propagate(self):
        from clean_v2.providers import ProviderWireFailure

        for exc in (ValueError("schema drift"), KeyError("x"), ProviderWireFailure("http_400", http_status=400)):
            with self.subTest(exc=repr(exc)):
                with self.assertRaises(type(exc)):
                    self._run(exc)

    def test_standard_path_unchanged_for_outage(self):
        from clean_v2.providers import ProviderWireFailure

        with self.assertRaises(ProviderWireFailure):
            self._run(ProviderWireFailure("http_429", http_status=429), simple=False)

    def test_mandatory_checks_still_block_during_tone_outage(self):
        from clean_v2.providers import ProviderWireFailure

        with self.assertRaises(pl.CleanV2FactualityContentBlock):
            self._run(ProviderWireFailure("http_503", http_status=503), fact_block=True)
        with self.assertRaises(pl.CleanV2ToneContentBlock):
            # objective religious-quote flag still blocks even though subjective ones do not
            def fact(**_):
                return {"status": "pass"}

            def tone(**_):
                raise pl.CleanV2ToneContentBlock({"unverified_religious_quote_flags": ["x"]})

            with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}), \
                    mock.patch.object(pl, "_run_legacy_factuality_audit", fact), \
                    mock.patch.object(pl, "_run_legacy_tone_naturalness_audit", tone):
                pl._run_text_audits(output_dir=Path(t), brief={}, plan={}, script={})


class SimpleModeVisualReportTests(unittest.TestCase):
    def test_report_counts_primary_extra_and_backgrounds_from_used_rows(self):
        rights = [
            {"provider": "pexels", "beat_id": "b1"},
            {"provider": "generated_local", "beat_id": "b2"},
            {"provider": "pixabay", "beat_id": "b3"},
            {"provider": "pexels", "beat_id": "b4", "pacing_auxiliary": True},
            {"provider": "generated_local", "beat_id": "b5", "pacing_auxiliary": True},
        ]
        with tempfile.TemporaryDirectory() as t:
            qa = simple_visual_qa(output_dir=Path(t), plan={}, script={}, rights=rights, fmt="film")
            self.assertEqual((qa["clips"], qa["stock_clips"], qa["fallback_backgrounds"]), (3, 2, 1))
            self.assertEqual((qa["extra_pacing_clips"], qa["extra_pacing_backgrounds"]), (2, 1))
            rep = write_simple_report(Path(t), fmt="film")
            self.assertEqual(rep["scenes"], 3)
            self.assertEqual(rep["stock_clips_selected_by_ranking"], 2)
            self.assertEqual(rep["fallback_backgrounds"], 1)
            self.assertEqual(rep["extra_pacing_clips"], 2)
            self.assertAlmostEqual(rep["fallback_share"], 0.333, places=3)
            low = rep["statement"].lower()
            self.assertIn("not reviewed", low)
            self.assertNotIn("matched", low)
            self.assertEqual(rep["publish_decision"], "owner")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class SimpleModeSanityTests(unittest.TestCase):
    def _make(self, path, video, audio):
        import subprocess

        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
             "-t", "4", "-shortest", "-pix_fmt", "yuv420p", str(path)], check=True)

    def test_black_and_silent_blocks_but_normal_passes(self):
        from clean_v2.simple_mode import SimpleModeSanityError, final_video_sanity

        with tempfile.TemporaryDirectory() as t:
            bad = Path(t) / "bad.mp4"
            good = Path(t) / "good.mp4"
            self._make(bad, "color=c=black:s=320x240:r=25", "anullsrc=r=44100:cl=mono")
            self._make(good, "testsrc=s=320x240:r=25", "sine=frequency=440:sample_rate=44100")
            with self.assertRaises(SimpleModeSanityError):
                final_video_sanity(bad, 4.0)
            self.assertEqual(final_video_sanity(good, 4.0)["status"], "pass")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class SimpleModeEndToEndTests(unittest.TestCase):
    def test_pipeline_completes_with_simple_gates_and_honest_report(self):
        from scripts import test_clean_v2 as base

        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {SIMPLE_MODE_ENV: "1"}):
            root = Path(t)
            brief = base._brief()
            brief_path = root / "approved-brief.json"
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            out = root / "out"
            pipeline = pl.CleanV2Pipeline(
                router=base._FakeRouter(),
                voice_synthesizer=base._FakeVoice(),
                visual_source=base._FakeVisuals(),
                visual_qa=simple_visual_qa,
                opening_director=simple_opening_director,
                cinematic_layer=base._passing_cinematic_layer,
                final_master_qc=base._passing_final_master_qc,
                text_audit=base._passing_text_audit,
                audio_mastering=base._passing_audio_mastering,
                narrative_identity=base._passing_narrative_identity,
            )
            result = pipeline.run(
                brief_path=brief_path,
                approved_sha256=base.compute_brief_sha256(brief),
                output_dir=out,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=2,
            )
            self.assertEqual(result["status"], "pass")
            self.assertTrue((out / "final.mp4").is_file())
            manifest = json.loads((out / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_mode"], "simple")
            self.assertEqual(manifest["opening_director_status"], "skipped_simple_mode")
            report = json.loads((out / "simple-mode-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["semantic_visual_review"], "not_performed")


if __name__ == "__main__":
    unittest.main()
