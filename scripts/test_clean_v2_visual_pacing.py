from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.contracts import compute_brief_sha256
from clean_v2.pipeline import CINEMATIC_STAGE, CleanV2Pipeline, _estimate_section_seconds
from clean_v2 import media as media_module


def _stub_engine_color_modules(filter_string: str) -> dict[str, types.ModuleType]:
    """Build the minimal fake isco_video_agent.media.color module tree so
    media.py's lazy `from isco_video_agent.media.color import
    build_color_filter` succeeds without a real Engine checkout - matching
    the stubbing convention already used in test_clean_v2_final_master_qc.py
    for this exact "test job has no Engine" situation."""
    package = types.ModuleType("isco_video_agent")
    package.__path__ = []
    media_pkg = types.ModuleType("isco_video_agent.media")
    media_pkg.__path__ = []
    color_mod = types.ModuleType("isco_video_agent.media.color")
    color_mod.build_color_filter = lambda _path: filter_string
    return {
        "isco_video_agent": package,
        "isco_video_agent.media": media_pkg,
        "isco_video_agent.media.color": color_mod,
    }


def _candidate(provider: str, asset_id: str) -> dict:
    return {
        "provider": provider,
        "asset_id": asset_id,
        "download_url": f"https://media.invalid/{provider}/{asset_id}.mp4",
        "source_url": f"https://source.invalid/{provider}/{asset_id}",
        "creator": "test",
        "creator_url": "",
        "query": "quiet desk notebook wide shot",
    }


def _pacing_plan(*section_ids: str) -> dict:
    return {
        "sections": [
            {
                "id": section_id,
                "heading": "h",
                "purpose": "p",
                "visual_query_en": "quiet desk notebook wide shot",
            }
            for section_id in section_ids
        ]
    }


class StockVisualSourceAcquirePacingTests(unittest.TestCase):
    def test_long_section_acquires_extra_same_query_clips(self) -> None:
        # A ~70s estimated duration for one section, well past
        # PACING_MAX_SHOT_SECONDS (22s): ceil(70/22) = 4, capped at
        # PACING_MAX_SHOTS_PER_SECTION (3).
        source = media_module.StockVisualSource()
        pexels_candidates = [
            _candidate("pexels", "p1"),
            _candidate("pexels", "p2"),
            _candidate("pexels", "p3"),
        ]

        def fake_pexels(_query, *, portrait):
            del portrait
            return pexels_candidates.pop(0) if pexels_candidates else None

        def fake_download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"V" * 4096)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_pexels", side_effect=fake_pexels
        ), mock.patch.object(
            source, "_pixabay", return_value=None
        ), mock.patch.object(
            media_module, "_download_media", side_effect=fake_download
        ):
            clips, rights = source.acquire(
                _pacing_plan("s1"),
                Path(root),
                "film",
                5,
                section_estimated_seconds={"s1": 70.0},
            )

        self.assertEqual(len(clips), 3)
        self.assertEqual(len(rights), 3)
        self.assertTrue(all(row["section_id"] == "s1" for row in rights))
        self.assertFalse(rights[0].get("pacing_auxiliary"))
        self.assertTrue(rights[1].get("pacing_auxiliary"))
        self.assertTrue(rights[2].get("pacing_auxiliary"))

    def test_normal_section_is_unaffected(self) -> None:
        source = media_module.StockVisualSource()
        pexels_candidates = [_candidate("pexels", "p1"), _candidate("pexels", "p2")]

        def fake_pexels(_query, *, portrait):
            del portrait
            return pexels_candidates.pop(0) if pexels_candidates else None

        def fake_download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"V" * 4096)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_pexels", side_effect=fake_pexels
        ), mock.patch.object(
            source, "_pixabay", return_value=None
        ), mock.patch.object(
            media_module, "_download_media", side_effect=fake_download
        ):
            # A 12s flat slot is under PACING_MAX_SHOT_SECONDS (22s): exactly
            # today's behavior, one clip, no auxiliary tag.
            clips, rights = source.acquire(
                _pacing_plan("s1"),
                Path(root),
                "film",
                5,
                section_estimated_seconds={"s1": 12.0},
            )

        self.assertEqual(len(clips), 1)
        self.assertFalse(rights[0].get("pacing_auxiliary"))

    def test_no_slot_hint_behaves_exactly_as_before(self) -> None:
        source = media_module.StockVisualSource()

        def fake_pexels(_query, *, portrait):
            del portrait
            return _candidate("pexels", "p1")

        def fake_download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"V" * 4096)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_pexels", side_effect=fake_pexels
        ), mock.patch.object(
            media_module, "_download_media", side_effect=fake_download
        ):
            clips, rights = source.acquire(_pacing_plan("s1"), Path(root), "film", 5)

        self.assertEqual(len(clips), 1)
        self.assertFalse(rights[0].get("pacing_auxiliary"))

    def test_minimum_shot_seconds_floor_reduces_extra_shot_count(self) -> None:
        # A 40s flat slot: ceil(40/22)=2 desired, but 40/3 < 3.5 would be the
        # third split - the floor must keep this at 2, not 3.
        source = media_module.StockVisualSource()
        pexels_candidates = [
            _candidate("pexels", "p1"),
            _candidate("pexels", "p2"),
            _candidate("pexels", "p3"),
        ]

        def fake_pexels(_query, *, portrait):
            del portrait
            return pexels_candidates.pop(0) if pexels_candidates else None

        def fake_download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"V" * 4096)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_pexels", side_effect=fake_pexels
        ), mock.patch.object(
            source, "_pixabay", return_value=None
        ), mock.patch.object(
            media_module, "_download_media", side_effect=fake_download
        ):
            clips, _rights = source.acquire(
                _pacing_plan("s1"),
                Path(root),
                "film",
                5,
                section_estimated_seconds={"s1": 40.0},
            )

        self.assertEqual(len(clips), 2)

    def test_extra_shots_stop_gracefully_when_stock_is_exhausted(self) -> None:
        source = media_module.StockVisualSource()
        # Only the primary candidate is available; extras must be skipped,
        # never raise.
        pexels_candidates = [_candidate("pexels", "p1")]

        def fake_pexels(_query, *, portrait):
            del portrait
            return pexels_candidates.pop(0) if pexels_candidates else None

        def fake_download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"V" * 4096)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_pexels", side_effect=fake_pexels
        ), mock.patch.object(
            source, "_pixabay", return_value=None
        ), mock.patch.object(
            media_module, "_download_media", side_effect=fake_download
        ):
            clips, rights = source.acquire(
                _pacing_plan("s1"),
                Path(root),
                "film",
                5,
                section_estimated_seconds={"s1": 70.0},
            )

        self.assertEqual(len(clips), 1)
        self.assertFalse(rights[0].get("pacing_auxiliary"))

    def test_flat_slot_applies_independently_to_every_section(self) -> None:
        # A per-section estimated duration is supplied independently for
        # each section - two sections given the same estimate both split
        # the same way, but each section's own value drives its own split.
        source = media_module.StockVisualSource()
        pexels_candidates = [
            _candidate("pexels", f"p{index}") for index in range(1, 7)
        ]

        def fake_pexels(_query, *, portrait):
            del portrait
            return pexels_candidates.pop(0) if pexels_candidates else None

        def fake_download(_url, destination):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"V" * 4096)

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            source, "_pexels", side_effect=fake_pexels
        ), mock.patch.object(
            source, "_pixabay", return_value=None
        ), mock.patch.object(
            media_module, "_download_media", side_effect=fake_download
        ):
            clips, rights = source.acquire(
                _pacing_plan("s1", "s2"),
                Path(root),
                "film",
                5,
                section_estimated_seconds={"s1": 45.0, "s2": 45.0},
            )

        self.assertEqual(len(clips), 6)
        by_section: dict[str, int] = {}
        for row in rights:
            by_section[row["section_id"]] = by_section.get(row["section_id"], 0) + 1
        self.assertEqual(by_section, {"s1": 3, "s2": 3})


class SectionDurationEstimationTests(unittest.TestCase):
    """Test Requirement A: a section's estimated duration comes from its own
    narration length, not an equal flat share of the total."""

    @staticmethod
    def _sections(*ids: str) -> list[dict]:
        return [{"id": section_id} for section_id in ids]

    def test_longer_narration_section_gets_a_larger_share_than_flat(self) -> None:
        sections = self._sections("s1", "s2", "s3")
        script = {
            "sections": [
                {"id": "s1", "narration": "قصير جدًا"},
                {"id": "s2", "narration": "نص أطول بوضوح يحمل تفاصيل أكثر بكثير من الجملة الأولى القصيرة"},
                {"id": "s3", "narration": "نص متوسط الطول"},
            ]
        }
        estimated = _estimate_section_seconds(sections, script, 90.0)

        flat_share = 90.0 / 3
        self.assertGreater(estimated["s2"], flat_share)
        self.assertGreater(estimated["s2"], estimated["s1"])
        self.assertGreater(estimated["s2"], estimated["s3"])
        self.assertLess(estimated["s1"], flat_share)
        self.assertAlmostEqual(sum(estimated.values()), 90.0, places=6)

    def test_near_equal_length_narration_sections_split_close_to_evenly(self) -> None:
        sections = self._sections("s1", "s2")
        script = {
            "sections": [
                {"id": "s1", "narration": "نص عادي بطول معتدل هنا الآن"},
                {"id": "s2", "narration": "نص آخر قريب جدًا من نفس الطول"},
            ]
        }
        estimated = _estimate_section_seconds(sections, script, 40.0)
        # Character counts (27 vs 29) are close but not identical, so this
        # only guards against a gross regression back to a hard 50/50 split.
        self.assertAlmostEqual(estimated["s1"], 20.0, delta=3.0)
        self.assertAlmostEqual(sum(estimated.values()), 40.0, places=6)

    def test_last_section_absorbs_rounding_residual(self) -> None:
        sections = self._sections("s1", "s2", "s3")
        script = {
            "sections": [
                {"id": "s1", "narration": "أ" * 7},
                {"id": "s2", "narration": "ب" * 11},
                {"id": "s3", "narration": "ج" * 13},
            ]
        }
        estimated = _estimate_section_seconds(sections, script, 33.333333)
        self.assertAlmostEqual(sum(estimated.values()), 33.333333, places=6)

    def test_all_empty_narration_fails_clearly_instead_of_dividing_by_zero(
        self,
    ) -> None:
        sections = self._sections("s1", "s2")
        script = {
            "sections": [
                {"id": "s1", "narration": "   "},
                {"id": "s2", "narration": ""},
            ]
        }
        with self.assertRaisesRegex(
            RuntimeError, "no narration text found for any section"
        ):
            _estimate_section_seconds(sections, script, 60.0)


class SectionSlotDurationsTests(unittest.TestCase):
    def test_missing_manifest_falls_back_to_uniform_split(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root)
            paths = [output_dir / "a.mp4", output_dir / "b.mp4"]
            durations = media_module._section_slot_durations(
                output_dir, paths, 20.0
            )
        self.assertEqual(len(durations), 2)
        self.assertAlmostEqual(durations[0], durations[1])
        self.assertAlmostEqual(durations[0], 10.0 + 0.12)

    def test_manifest_subdivides_only_the_long_section_share(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root)
            (output_dir / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {"local_file": "a.mp4", "section_id": "s1"},
                            {"local_file": "b.mp4", "section_id": "s1"},
                            {"local_file": "c.mp4", "section_id": "s2"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            paths = [
                output_dir / "a.mp4",
                output_dir / "b.mp4",
                output_dir / "c.mp4",
            ]
            # Two logical sections share 30s total flat -> 15s each; s1's
            # clips (a, b) split its 15s in half, s2's single clip (c) keeps
            # the full 15s.
            durations = media_module._section_slot_durations(
                output_dir, paths, 30.0
            )
        self.assertAlmostEqual(durations[0], 7.5 + 0.12)
        self.assertAlmostEqual(durations[1], 7.5 + 0.12)
        self.assertAlmostEqual(durations[2], 15.0 + 0.12)

    def test_manifest_estimated_seconds_drives_unequal_section_shares(self) -> None:
        # s1 (2 clips) is estimated at 50s, s2 (1 clip) at only 10s - a real
        # narration-weighted split, not an equal flat share.
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root)
            (output_dir / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {"local_file": "a.mp4", "section_id": "s1"},
                            {"local_file": "b.mp4", "section_id": "s1"},
                            {"local_file": "c.mp4", "section_id": "s2"},
                        ],
                        "estimated_section_seconds": {"s1": 50.0, "s2": 10.0},
                    }
                ),
                encoding="utf-8",
            )
            paths = [
                output_dir / "a.mp4",
                output_dir / "b.mp4",
                output_dir / "c.mp4",
            ]
            durations = media_module._section_slot_durations(
                output_dir, paths, 60.0
            )
        self.assertAlmostEqual(durations[0], 25.0 + 0.12)
        self.assertAlmostEqual(durations[1], 25.0 + 0.12)
        self.assertAlmostEqual(durations[2], 10.0 + 0.12)

    def test_manifest_estimated_seconds_renormalizes_to_a_different_total(
        self,
    ) -> None:
        # The render call site may hand this a smaller time budget than the
        # sum of the raw per-section estimates (e.g. "remaining" seconds
        # after the opening's fixed 7/11/12s slots) - the real per-section
        # weights must still be honored proportionally against whatever
        # total is actually being distributed.
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root)
            (output_dir / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {"local_file": "a.mp4", "section_id": "s1"},
                            {"local_file": "b.mp4", "section_id": "s2"},
                        ],
                        "estimated_section_seconds": {"s1": 80.0, "s2": 20.0},
                    }
                ),
                encoding="utf-8",
            )
            paths = [output_dir / "a.mp4", output_dir / "b.mp4"]
            # raw_total=100 renormalized onto a 50s budget: s1 keeps 4x s2.
            durations = media_module._section_slot_durations(
                output_dir, paths, 50.0
            )
        self.assertAlmostEqual(durations[0], 40.0 + 0.12)
        self.assertAlmostEqual(durations[1], 10.0 + 0.12)
        self.assertAlmostEqual(sum(durations) - 2 * 0.12, 50.0, places=6)

    def test_manifest_missing_a_section_key_falls_back_to_flat_split(self) -> None:
        # estimated_section_seconds exists but doesn't cover every section
        # actually present among the clips - never partially trust it.
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root)
            (output_dir / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {"local_file": "a.mp4", "section_id": "s1"},
                            {"local_file": "b.mp4", "section_id": "s2"},
                        ],
                        "estimated_section_seconds": {"s1": 50.0},
                    }
                ),
                encoding="utf-8",
            )
            paths = [output_dir / "a.mp4", output_dir / "b.mp4"]
            durations = media_module._section_slot_durations(
                output_dir, paths, 20.0
            )
        self.assertAlmostEqual(durations[0], 10.0 + 0.12)
        self.assertAlmostEqual(durations[1], 10.0 + 0.12)

    def test_last_clip_in_a_section_absorbs_the_rounding_residual(self) -> None:
        # s1's 10.0s share split across 3 clips is a repeating decimal - the
        # last of the three must absorb the residual so the section's own
        # clips sum to exactly 10.0, not a value drifted by float rounding.
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root)
            (output_dir / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {"local_file": "a.mp4", "section_id": "s1"},
                            {"local_file": "b.mp4", "section_id": "s1"},
                            {"local_file": "c.mp4", "section_id": "s1"},
                        ],
                        "estimated_section_seconds": {"s1": 10.0},
                    }
                ),
                encoding="utf-8",
            )
            paths = [
                output_dir / "a.mp4",
                output_dir / "b.mp4",
                output_dir / "c.mp4",
            ]
            durations = media_module._section_slot_durations(
                output_dir, paths, 10.0, pad=0.0
            )
        self.assertAlmostEqual(sum(durations), 10.0, places=9)
        self.assertAlmostEqual(durations[0], durations[1], places=9)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class RenderVideoExplicitPacingTests(unittest.TestCase):
    @staticmethod
    def _make_clip(path: Path, color: str) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x180:r=30:d=5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-y",
                str(path),
            ],
            check=True,
        )

    @staticmethod
    def _make_narration(path: Path, seconds: float) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:sample_rate=24000:duration={seconds}",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(path),
            ],
            check=True,
        )

    def test_render_honors_manifest_driven_section_split(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root)
            visuals_dir = output_dir / "visuals"
            visuals_dir.mkdir()
            clip_a = visuals_dir / "a.mp4"
            clip_b = visuals_dir / "b.mp4"
            clip_c = visuals_dir / "c.mp4"
            self._make_clip(clip_a, "#172033")
            self._make_clip(clip_b, "#6d4c41")
            self._make_clip(clip_c, "#2e7d32")
            narration_path = output_dir / "narration.wav"
            self._make_narration(narration_path, 30.0)
            (output_dir / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {"local_file": "a.mp4", "section_id": "s1"},
                            {"local_file": "b.mp4", "section_id": "s1"},
                            {"local_file": "c.mp4", "section_id": "s2"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            final_path = output_dir / "final.mp4"
            media_module.render_video(
                narration_path, [clip_a, clip_b, clip_c], final_path, "film"
            )
            self.assertTrue(final_path.is_file())
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_format",
                    "-of",
                    "json",
                    str(final_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            duration = float(json.loads(probe.stdout)["format"]["duration"])
            self.assertAlmostEqual(duration, 30.0, delta=1.0)


def _brief() -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": "كيف تبدأ بخطوة صغيرة",
        "format": "film",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": "شرح عملي هادئ دون وعود مبالغ فيها.",
        "research_pack": [],
        "hard_constraints": ["No fabricated facts."],
    }


def _plan() -> dict:
    return {
        "title": "خطوة واحدة",
        "promise": "فهم طريقة عملية للبدء",
        "cta": "إذا كانت هذه الفكرة قريبة منك، اكتب تجربتك في التعليقات.",
        "sections": [
            {
                "id": section_id,
                "heading": heading,
                "purpose": "شرح مختصر",
                "visual_query_en": "quiet desk notebook wide shot",
            }
            for section_id, heading in (
                ("s1", "المشكلة"),
                ("s2", "الفكرة"),
                ("s3", "التطبيق"),
                ("s4", "المراجعة"),
                ("s5", "الاستمرار"),
            )
        ],
    }


def _script() -> dict:
    return {
        "title": "خطوة واحدة",
        "sections": [
            {
                "id": section_id,
                "narration": narration,
            }
            for section_id, narration in (
                ("s1", "نؤجل البداية أحيانًا لأن المهمة تبدو أكبر من اللحظة المتاحة أمامنا."),
                ("s2", "حين نصغر الفعل الأول يصبح البدء أوضح، ونختبر الواقع بدل أن نبقى داخل الخطة."),
                ("s3", "اختر اليوم خطوة يمكن تنفيذها الآن، ثم اترك النتيجة التالية لما بعد البداية."),
                ("s4", "بعد التنفيذ راجع ما حدث بهدوء، وما الذي جعل الخطوة ممكنة في هذه المرة."),
                ("s5", "ثبت ما نجح واختر خطوة تالية صغيرة وواضحة حتى يتحول التقدم إلى عادة عملية."),
            )
        ],
    }


class _FakeRouter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def route(self, *, stage, prompt, max_tokens, validator):
        del prompt, max_tokens
        value = _plan() if stage == "planning" else _script()
        self.events.append({"stage": stage, "provider": "fixture", "result": "success"})
        return validator(value)


class _LongFakeVoice:
    """Produces narration.wav with a controlled, exact duration so the test
    can assert the flat-slot value pipeline.py computes for acquire()."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.calls = 0
        self.last_provider = "nabra:af_msa"
        self.fallback_used = True

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        self.calls += 1
        if not transcript.strip():
            raise RuntimeError("empty fixture transcript")
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:sample_rate=24000:duration={self.seconds}",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(output_path),
            ],
            check=True,
        )
        return output_path


class _RecordingVisuals:
    """A visual_source fixture that records the section_estimated_seconds it
    was given and deterministically returns one auxiliary pacing clip for
    section s1 only, mirroring what the real StockVisualSource.acquire would
    produce for a long first section - without any real stock-provider I/O.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.received_section_estimated_seconds: dict[str, float] | None = None

    @staticmethod
    def _write_clip(path: Path, color: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x180:r=30:d=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-y",
                str(path),
            ],
            check=True,
        )

    def acquire(self, plan, output_dir, fmt, max_visuals, section_estimated_seconds=None):
        del fmt
        self.received_section_estimated_seconds = section_estimated_seconds
        sections = list(plan.get("sections") or [])[: max(1, int(max_visuals))]
        clips: list[Path] = []
        rights: list[dict] = []
        colors = ["#172033", "#6d4c41", "#2e7d32", "#4527a0", "#ad1457"]
        for index, section in enumerate(sections):
            section_id = str(section.get("id") or "")
            destination = output_dir / f"visual-{len(clips) + 1:02d}.mp4"
            self._write_clip(destination, colors[index % len(colors)])
            clips.append(destination)
            rights.append(
                {
                    "provider": "fixture",
                    "asset_id": f"primary-{section_id}",
                    "local_file": destination.name,
                    "section_id": section_id,
                }
            )
            if section_id == "s1":
                extra = output_dir / f"visual-{len(clips) + 1:02d}.mp4"
                self._write_clip(extra, "#ffab00")
                clips.append(extra)
                rights.append(
                    {
                        "provider": "fixture",
                        "asset_id": "aux-s1",
                        "local_file": extra.name,
                        "section_id": section_id,
                        "pacing_auxiliary": True,
                    }
                )
        return clips, rights


def _passing_text_audit(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {"schema_version": 1, "status": "pass", "unsupported_claims": []}
    (output_dir / "factuality-audit.json").write_text(json.dumps(report), encoding="utf-8")
    return report


def _passing_audio_mastering(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    narration_path = Path(kwargs["narration_path"])
    mastered_path = output_dir / "narration-mastered.wav"
    shutil.copyfile(narration_path, mastered_path)
    report = {"schema_version": 1, "status": "pass", "narration_file": mastered_path.name}
    (output_dir / "audio-mastering.json").write_text(json.dumps(report), encoding="utf-8")
    return report


def _passing_narrative_identity(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {
        "schema_version": 1,
        "opener": "أهلاً بكم من جديد في هذه الحلقة",
        "closer": "نلقاكم في حلقة قادمة",
        "transitions": ["بعد هذه الفكرة", "ولننتقل الآن", "وهنا يأتي السؤال"],
    }
    (output_dir / "narrative-identity.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    return report


def _passing_cinematic_layer(**kwargs) -> dict:
    output_dir = Path(kwargs["output_dir"])
    report = {"schema_version": 1, "layer": CINEMATIC_STAGE, "status": "pass"}
    (output_dir / "security-cinematic-v2.json").write_text(json.dumps(report), encoding="utf-8")
    return report


def _passing_final_master_qc(output_dir: Path) -> dict:
    report = {
        "schema_version": 1,
        "status": "pass",
        "final_media_mutated": False,
        "blocking_findings": [],
    }
    (output_dir / "final-master-qc.json").write_text(json.dumps(report), encoding="utf-8")
    return report


def _not_applicable_opening_director(**kwargs) -> dict:
    return {"schema_version": 1, "status": "not_applicable", "reason": "test_stub"}


class _RecordingVisualQA:
    def __init__(self) -> None:
        self.received_rights: list[dict] | None = None

    def __call__(self, **kwargs) -> dict:
        output_dir = Path(kwargs["output_dir"])
        self.received_rights = list(kwargs["rights"])
        report = {"schema_version": 1, "status": "pass", "final_media_mutated": False}
        (output_dir / "final-cut-visual-qa.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
        (output_dir / "visual-audit.json").write_text("[]", encoding="utf-8")
        return report


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class PipelineWiringTests(unittest.TestCase):
    def test_pipeline_computes_per_section_estimate_and_keeps_gates_primary_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brief_path = root / "approved-brief.json"
            brief = _brief()
            brief_path.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
            output = root / "output"

            visuals = _RecordingVisuals()
            visual_qa = _RecordingVisualQA()
            # Timeline First synthesizes multiple measured semantic voice units
            # (hook/prayer/channel identity/topic/outro). This fixture returns a fixed
            # 26s per synth call; the test must follow the measured voice-owned timeline,
            # not a historical hard-coded synth-call count.
            long_voice = _LongFakeVoice(26.0)
            pipeline = CleanV2Pipeline(
                router=_FakeRouter(),
                voice_synthesizer=long_voice,
                visual_source=visuals,
                visual_qa=visual_qa,
                opening_director=_not_applicable_opening_director,
                cinematic_layer=_passing_cinematic_layer,
                final_master_qc=_passing_final_master_qc,
                text_audit=_passing_text_audit,
                audio_mastering=_passing_audio_mastering,
                narrative_identity=_passing_narrative_identity,
            )
            result = pipeline.run(
                brief_path=brief_path,
                approved_sha256=compute_brief_sha256(brief),
                output_dir=output,
                engine_sha="a" * 40,
                runner_sha="b" * 40,
                max_visuals=5,
            )

            self.assertEqual(result["status"], "pass")
            # acquire() must receive a real per-section estimate keyed by
            # every section id, summing to the full narration duration - not
            # pipeline.py's exact character-count formula recomputed here
            # (the real script text passes through channel-persona/
            # human-feel rewriting before estimation, so its narration
            # length differs from the raw _script() fixture text; only the
            # contract - real per-section keys, unequal shares, exact total
            # - is stable enough to assert against).
            received = visuals.received_section_estimated_seconds
            self.assertIsNotNone(received)
            self.assertEqual(set(received), {"s1", "s2", "s3", "s4", "s5"})
            for seconds in received.values():
                self.assertGreater(seconds, 0.0)
            timeline = json.loads(
                (output / "timeline-first.json").read_text(encoding="utf-8")
            )
            self.assertEqual(timeline["timeline_owner"], "measured_charon_voice")
            self.assertAlmostEqual(
                sum(received.values()),
                float(timeline["voice_seconds_measured"]),
                places=3,
            )
            # Not a flat 26.0s-each split: sections have different narration
            # lengths, so their shares differ.
            self.assertGreater(max(received.values()) - min(received.values()), 1.0)

            manifest = json.loads(
                (output / "rights-manifest.json").read_text(encoding="utf-8")
            )
            assets = manifest["assets"]
            self.assertEqual(len(assets), 6)
            self.assertEqual(
                sum(1 for item in assets if item.get("pacing_auxiliary")), 1
            )

            # Visual QA now tolerates one-or-more assets per section, so it
            # sees the full rights list including the pacing auxiliary.
            self.assertEqual(len(visual_qa.received_rights), 6)
            self.assertEqual(
                sum(1 for item in visual_qa.received_rights if item.get("pacing_auxiliary")),
                1,
            )

            self.assertTrue((output / "final.mp4").is_file())


class ColorGradeIntegrationTests(unittest.TestCase):
    def test_grade_filter_is_appended_when_engine_color_module_is_importable(self) -> None:
        stub_filter = "eq=contrast=9.999:brightness=0.0100:saturation=0.9000"
        modules = _stub_engine_color_modules(stub_filter)
        with mock.patch.dict(sys.modules, modules):
            fragment = media_module._grade_clip_filter(Path("does-not-matter.mp4"))
        self.assertEqual(fragment, stub_filter)

    def test_grade_filter_is_empty_without_engine(self) -> None:
        # No stub installed and no real Engine on the path here - matches
        # the CI "test" job, which never checks out Engine at all.
        with mock.patch.dict(sys.modules, {"isco_video_agent.media.color": None}):
            fragment = media_module._grade_clip_filter(Path("does-not-matter.mp4"))
        self.assertEqual(fragment, "")

    def test_trim_and_grade_command_includes_the_grade_fragment(self) -> None:
        stub_filter = "eq=contrast=9.999:brightness=0.0100:saturation=0.9000"
        modules = _stub_engine_color_modules(stub_filter)
        captured: dict[str, list[str]] = {}

        def fake_run(command, *, timeout):
            del timeout
            captured["command"] = command
            return None

        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            sys.modules, modules
        ), mock.patch.object(media_module, "_run", side_effect=fake_run):
            media_module._trim_and_grade_clip(
                Path(root) / "source.mp4",
                Path(root) / "trimmed.mp4",
                width=1920,
                height=1080,
                seconds=6.0,
            )

        vf_index = captured["command"].index("-vf") + 1
        self.assertIn(stub_filter, captured["command"][vf_index])

    def test_trim_and_grade_command_omits_grading_without_engine(self) -> None:
        captured: dict[str, list[str]] = {}

        def fake_run(command, *, timeout):
            del timeout
            captured["command"] = command
            return None

        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            sys.modules, {"isco_video_agent.media.color": None}
        ), mock.patch.object(media_module, "_run", side_effect=fake_run):
            media_module._trim_and_grade_clip(
                Path(root) / "source.mp4",
                Path(root) / "trimmed.mp4",
                width=1920,
                height=1080,
                seconds=6.0,
            )

        vf_index = captured["command"].index("-vf") + 1
        vf = captured["command"][vf_index]
        self.assertNotIn("eq=contrast", vf)
        self.assertIn("trim=duration=6.000", vf)


class ReferenceColorMatchLiteTests(unittest.TestCase):
    def test_representative_reference_chooses_real_clip_near_median(self) -> None:
        measured = {
            "cold.mp4": media_module._RgbStats(70, 85, 120, 35, 36, 38),
            "middle.mp4": media_module._RgbStats(112, 108, 104, 42, 41, 40),
            "hot.mp4": media_module._RgbStats(170, 145, 105, 58, 54, 48),
        }
        self.assertEqual(
            media_module._representative_reference(measured),
            "middle.mp4",
        )

    def test_reference_match_filter_is_bounded_and_clip_constant(self) -> None:
        source = media_module._RgbStats(70, 180, 90, 12, 80, 18)
        reference = media_module._RgbStats(130, 120, 115, 60, 30, 45)
        fragment = media_module._reference_match_filter(source, reference)
        self.assertTrue(fragment.startswith("lutrgb="))
        self.assertEqual(fragment.count("clip(val*"), 3)
        self.assertIn("r='", fragment)
        self.assertIn("g='", fragment)
        self.assertIn("b='", fragment)
        # One filter string is calculated once for the clip: there is no
        # frame/time expression that could pump the grade during playback.
        self.assertNotIn(" t ", fragment)
        self.assertNotIn("n)", fragment)

    def test_reference_color_plan_reports_zero_ai_and_one_real_reference(self) -> None:
        paths = [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")]
        values = {
            "a.mp4": media_module._RgbStats(80, 90, 100, 30, 30, 30),
            "b.mp4": media_module._RgbStats(110, 108, 105, 40, 39, 38),
            "c.mp4": media_module._RgbStats(160, 140, 115, 55, 52, 48),
        }

        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            media_module,
            "_sample_rgb_stats",
            side_effect=lambda path: values[path.name],
        ):
            filters = media_module._build_reference_color_plan(paths, Path(root))
            report = json.loads((Path(root) / "color-match.json").read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["reference_file"], "b.mp4")
        self.assertEqual(report["provider_calls_added"], 0)
        self.assertEqual(report["ai_calls_added"], 0)
        self.assertEqual(filters["b.mp4"], "")
        self.assertTrue(filters["a.mp4"].startswith("lutrgb="))
        self.assertTrue(filters["c.mp4"].startswith("lutrgb="))

    def test_master_lut_has_expected_cube_shape(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = media_module._write_master_look_lut(Path(root) / "look.cube")
            lines = path.read_text(encoding="ascii").splitlines()
        self.assertEqual(lines[0], 'TITLE "Isco Warm Neutral Master v1"')
        self.assertEqual(lines[1], f"LUT_3D_SIZE {media_module.MASTER_LOOK_LUT_SIZE}")
        self.assertEqual(
            len(lines),
            4 + (media_module.MASTER_LOOK_LUT_SIZE ** 3),
        )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class MasterLutFfmpegTests(unittest.TestCase):
    def test_generated_cube_is_accepted_by_ffmpeg_lut3d(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source = root_path / "source.mp4"
            dest = root_path / "dest.mp4"
            lut = media_module._write_master_look_lut(root_path / "look.cube")
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=#6d4c41:s=160x90:r=10:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(source),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source),
                    "-vf", f"lut3d=file='{media_module._ffmpeg_filter_path(lut)}':interp=tetrahedral",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest),
                ],
                check=True,
            )
            self.assertTrue(dest.is_file())
            self.assertGreater(dest.stat().st_size, 1000)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class DissolvePairTests(unittest.TestCase):
    @staticmethod
    def _make_clip(path: Path, color: str, seconds: float) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x180:r=30:d={seconds}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(path),
            ],
            check=True,
        )

    def test_dissolve_preserves_total_duration(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            left = root_path / "left.mp4"
            right = root_path / "right.mp4"
            self._make_clip(left, "#172033", 5.0)
            self._make_clip(right, "#6d4c41", 6.0)
            dest = root_path / "dissolved.mp4"
            media_module._dissolve_pair(left, right, dest)
            self.assertTrue(dest.is_file())
            merged_seconds = media_module.probe_duration(dest)
            self.assertAlmostEqual(merged_seconds, 11.0, delta=0.2)

    def test_dissolve_rejects_clips_too_short_for_crossfade(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            left = root_path / "left.mp4"
            right = root_path / "right.mp4"
            self._make_clip(left, "#172033", 0.2)
            self._make_clip(right, "#6d4c41", 5.0)
            dest = root_path / "dissolved.mp4"
            with self.assertRaisesRegex(
                RuntimeError, "pacing_dissolve_pair_too_short_for_timing_preserving_crossfade"
            ):
                media_module._dissolve_pair(left, right, dest)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class SectionBodySegmentsTests(unittest.TestCase):
    @staticmethod
    def _make_clip(path: Path, color: str, seconds: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x180:r=30:d={seconds}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(path),
            ],
            check=True,
        )

    def test_multi_clip_section_dissolves_into_one_segment_single_stays_plain(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source_dir = root_path / "sources"
            clip_a = source_dir / "a.mp4"
            clip_b = source_dir / "b.mp4"
            clip_c = source_dir / "c.mp4"
            self._make_clip(clip_a, "#172033", 6.0)
            self._make_clip(clip_b, "#6d4c41", 6.0)
            self._make_clip(clip_c, "#2e7d32", 8.0)
            paths = [clip_a, clip_b, clip_c]
            durations = [6.0, 6.0, 8.0]
            section_ids = ["s1", "s1", "s2"]

            work_dir = root_path / "work"
            segments = media_module._build_section_body_segments(
                work_dir, paths, durations, section_ids, width=320, height=180
            )

            # Two logical sections in, two segments out: s1's two clips merge
            # into one dissolved segment, s2's single clip stays its own.
            self.assertEqual(len(segments), 2)
            for segment in segments:
                self.assertTrue(segment.is_file())
            s1_seconds = media_module.probe_duration(segments[0])
            self.assertAlmostEqual(s1_seconds, 12.0, delta=0.3)
            s2_seconds = media_module.probe_duration(segments[1])
            self.assertAlmostEqual(s2_seconds, 8.0, delta=0.3)


class RenderVideoColorAndCutTests(unittest.TestCase):
    """Test Requirement D (color grade coverage including Opening) and the
    cross-section half of Requirement E (hard cut stays default without
    fabricated M9 evidence)."""

    @staticmethod
    def _opening_fixture(root: Path) -> list[Path]:
        paths = [
            root / "opening-cold_open.mp4",
            root / "opening-escalation.mp4",
            root / "body1.mp4",
            root / "body2.mp4",
        ]
        for path in paths:
            path.write_bytes(b"x" * 2048)
        (root / "opening-director.json").write_text(
            json.dumps(
                {
                    "status": "pass",
                    "mode": "legacy_first_30_three_audited_shots",
                    "slots": [
                        {"local_file": "opening-cold_open.mp4", "seconds": 7.0},
                        {"local_file": "opening-escalation.mp4", "seconds": 11.0},
                        {"local_file": "body1.mp4", "seconds": 12.0},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return paths

    def test_opening_and_body_clips_both_receive_the_color_grade(self) -> None:
        stub_filter = "eq=contrast=9.999:brightness=0.0100:saturation=0.9000"
        modules = _stub_engine_color_modules(stub_filter)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._opening_fixture(root)
            narration = root / "voice.wav"
            output = root / "final.mp4"
            captured: list[list[str]] = []

            def fake_run(command, *, timeout):
                captured.append(command)
                return None

            with mock.patch.dict(sys.modules, modules), mock.patch(
                "clean_v2.media.probe_duration", return_value=120.0
            ), mock.patch("clean_v2.media._run", side_effect=fake_run):
                media_module.render_video(narration, paths, output, "film")

            final_command = captured[-1]
            filters = final_command[final_command.index("-filter_complex") + 1]
            # The opening's own 7/11/12s timing is untouched...
            self.assertIn("trim=duration=7.000", filters)
            self.assertIn("trim=duration=11.000", filters)
            self.assertIn("trim=duration=12.000", filters)
            # ...but all three opening inputs (7s/11s/12s slots) now also
            # carry the same grade fragment as the body clips instead of
            # being excluded from it.
            self.assertEqual(filters.count(stub_filter), 3)
            pretrim_calls = [
                command
                for command in captured
                if "-vf" in command
                and stub_filter in command[command.index("-vf") + 1]
            ]
            self.assertGreaterEqual(len(pretrim_calls), 1)

    def test_grade_absent_from_opening_filters_without_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._opening_fixture(root)
            narration = root / "voice.wav"
            output = root / "final.mp4"
            captured: list[list[str]] = []

            def fake_run(command, *, timeout):
                captured.append(command)
                return None

            with mock.patch.dict(
                sys.modules, {"isco_video_agent.media.color": None}
            ), mock.patch(
                "clean_v2.media.probe_duration", return_value=120.0
            ), mock.patch("clean_v2.media._run", side_effect=fake_run):
                media_module.render_video(narration, paths, output, "film")

            final_command = captured[-1]
            filters = final_command[final_command.index("-filter_complex") + 1]
            self.assertNotIn("eq=contrast", filters)
            self.assertIn("trim=duration=7.000", filters)
            self.assertIn("trim=duration=12.000", filters)

    def test_cross_section_boundary_stays_a_hard_cut_without_fabricated_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            narration = root / "voice.wav"
            output = root / "final.mp4"
            paths = [root / "a.mp4", root / "b.mp4"]
            for path in paths:
                path.write_bytes(b"x" * 2048)
            (root / "rights-manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {"local_file": "a.mp4", "section_id": "s1"},
                            {"local_file": "b.mp4", "section_id": "s2"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            captured: list[list[str]] = []

            def fake_run(command, *, timeout):
                captured.append(command)
                return None

            with mock.patch.dict(
                sys.modules, {"isco_video_agent.media.color": None}
            ), mock.patch(
                "clean_v2.media.probe_duration", return_value=20.0
            ), mock.patch("clean_v2.media._run", side_effect=fake_run):
                media_module.render_video(narration, paths, output, "film")

            final_command = captured[-1]
            filters = final_command[final_command.index("-filter_complex") + 1]
            # No xfade anywhere in the final concat: two different sections,
            # each its own single-clip segment, joined by a plain concat -
            # Clean V2 has no real M9 continuity evidence (transition_intent
            # / continuity_role / motifs) to justify a dissolve at a section
            # boundary, so it stays a hard cut rather than fabricating one.
            self.assertNotIn("xfade", filters)
            self.assertIn("concat=n=2", filters)


if __name__ == "__main__":
    unittest.main()
