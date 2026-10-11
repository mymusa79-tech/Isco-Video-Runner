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
