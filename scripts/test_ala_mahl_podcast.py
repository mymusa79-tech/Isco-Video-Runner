"""Opt-in Ala Mahl podcast regression: imported ZIP, voices and identity are isolated."""
from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from clean_v2 import identity_sequence as identity, media, owner_pack
from clean_v2.pipeline import _voice_performance_mode_for_brief
from clean_v2.production_package import validate_manifest
from scripts.test_production_package import imported_pack, manifest


def _wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\\0\\0" * 7000)
    return path


def _identity_video(path: Path, *, seconds: float, size: str = "1280x720", audio: bool = True) -> Path:
    """Tiny real MP4 so the approved-video validator is exercised by real ffprobe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", f"color=c=0x2b1f16:s={size}:r=30:d={seconds}"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac", "-shortest"]
    subprocess.run(command + [str(path)], check=True, timeout=60)
    return path


def _ala_mahl_doc():
    doc = manifest("podcast", template="ala_mahl_dialogue")
    doc["script"]["sections"][0]["turns"][0]["text"] = "لماذا نحكم على صديق اعتذر عن مساعدتنا في أصعب أيامنا، قبل أن نعرف ما الذي كان يمر به، وهل كان يستطيع فعل شيء آخر؟"
    doc["scenes"][0]["start_text"] = "لماذا نحكم على صديق اعتذر عن مساعدتنا في أصعب أيامنا، قبل أن نعرف ما الذي كان يمر به، وهل كان يستطيع فعل شيء آخر؟"
    doc["publishing"]["title"] = "موقف من وجهين | على مهل"
    doc["publishing"]["description"] = "بودكاست على مهل عن موقف من الحياة اليومية."
    doc["publishing"]["hashtags"] = ["#على_مهل"]
    return doc


class AlaMahlPodcastTests(unittest.TestCase):
    def test_new_template_accepts_same_safe_podcast_contract(self):
        new_doc = _ala_mahl_doc()
        old_doc = manifest("podcast")
        self.assertEqual(validate_manifest(new_doc)["template"], "ala_mahl_dialogue")
        self.assertEqual(validate_manifest(old_doc)["template"], "dialogue_qa")
        bad = manifest("podcast", template="made_up")
        with self.assertRaises(Exception):
            validate_manifest(bad)

    def test_import_rejects_other_channel_publishing_metadata(self):
        import copy
        own = _ala_mahl_doc()
        own["publishing"]["title"] = "موقف من وجهين | على مهل"
        own["publishing"]["description"] = "بودكاست على مهل عن موقف من الحياة."
        own["publishing"]["hashtags"] = ["#على_مهل"]
        self.assertEqual(validate_manifest(own)["template"], "ala_mahl_dialogue")
        for field, value in (
            ("title", "تجربة من خارج النص"),
            ("description", "شاهد نداء اليقظة"),
            ("hashtags", ["#خارج_النص"]),
        ):
            bad = copy.deepcopy(own)
            bad["publishing"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(Exception, "قناة أخرى"):
                validate_manifest(bad)
        bad_child = copy.deepcopy(own)
        bad_child["derivative"] = {
            "start_text": "سؤال عن قرار",
            "end_text": "والمسؤولية.",
            "publishing": {
                "title": "مقطع من نداء اليقظة",
                "description": "ملخص",
                "hashtags": ["#Shorts"],
            },
        }
        with self.assertRaisesRegex(Exception, "قناة أخرى"):
            validate_manifest(bad_child)
        old = manifest("podcast")
        self.assertEqual(validate_manifest(old)["template"], "dialogue_qa")

    def test_owner_zip_build_uses_legacy_dialogue_engine_without_rewriting(self):
        doc = _ala_mahl_doc()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = owner_pack.build_artifacts(imported_pack(doc), {"format": "podcast"}, root)
            self.assertEqual(len(script["sections"]), 2)
            self.assertIn("A:", script["sections"][0]["narration"])
            self.assertIn("B:", script["sections"][0]["narration"])
            self.assertEqual(__import__("json").loads((root / "production.json").read_text())["template"],
                             "ala_mahl_dialogue")
            self.assertEqual(__import__("json").loads((root / "plan.json").read_text())["narrative_format"],
                             "dialogue_qa")

    def test_two_perspective_A_can_make_a_real_objection(self):
        from clean_v2.pipeline import (
            _validate_ala_mahl_podcast_dialogue_script,
            _validate_podcast_listener_proxy_script,
        )
        long_objection = "لماذا نفترض أنه مخطئ؟ " + " ".join(["ربما كان لديه سبب"] * 8) + "؟"
        sections = [
            {"narration": "A: لماذا نحكم على صديق اعتذر عن مساعدتنا في أصعب أيامنا، قبل أن نعرف ما الذي كان يمر به، وهل كان يستطيع فعل شيء آخر؟ B: " + " ".join(["ربما خاف من عدم القدرة."] * 14)},
            {"narration": "A: " + long_objection + " B: " + " ".join(["هنا علينا أن نسأل عن الملابسات."] * 14)},
        ]
        report = _validate_ala_mahl_podcast_dialogue_script({"sections": sections})
        self.assertEqual(report["voices"]["A"], "Iapetus")
        with self.assertRaisesRegex(RuntimeError, "question_too_long"):
            _validate_podcast_listener_proxy_script({"sections": sections})

    def test_new_podcast_hook_has_15_to_25_words_only(self):
        from clean_v2.pipeline import _validate_ala_mahl_podcast_dialogue_script
        valid = "لماذا نحكم على صديق اعتذر عن مساعدتنا في أصعب أيامنا، قبل أن نعرف ما الذي كان يمر به، وهل كان يستطيع فعل شيء آخر؟"
        self.assertEqual(len(valid.split()), 24)
        def candidate(opening):
            return {"sections": [{"narration": "A: " + opening + " B: "
                + " ".join(["تحتاج الصورة إلى تفاصيل قبل الحكم."] * 15)},
                {"narration": "A: وهل يمكن اختبار هذا؟ B: "
                + " ".join(["بالتأكيد، علينا النظر إلى الظروف كاملة."] * 12)}]}
        self.assertEqual(_validate_ala_mahl_podcast_dialogue_script(
            candidate(valid))["status"], "pass")
        for bad in ("لماذا رفض طلب صديقه؟",
                    valid + " وأكثر وأكثر وأكثر دون تفاصيل موثقة أو جديدة؟"):
            with self.subTest(bad=bad), self.assertRaisesRegex(
                RuntimeError, "ala_mahl_podcast_hook_requires_15_to_25_words"
            ):
                _validate_ala_mahl_podcast_dialogue_script(candidate(bad))

    def test_release_and_derived_short_never_inherit_other_channel_branding(self):
        from scripts import clean_v2_release_delivery as release
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "production.json").write_text(json.dumps({
                "format": "podcast", "template": "ala_mahl_dialogue"
            }, ensure_ascii=False), encoding="utf-8")
            (root / "plan.json").write_text(json.dumps({
                "title": "فهم الموقف",
                "sections": [{"id": "s1", "heading": "تفاصيل الموقف"}],
            }, ensure_ascii=False), encoding="utf-8")
            (root / "podcast-short.json").write_text(json.dumps({
                "section_id": "s1", "text": "لماذا رفض المساعدة؟"
            }, ensure_ascii=False), encoding="utf-8")
            published = release._derived_short_publish_metadata(
                root, parent_kind="podcast",
                parent={"title": "فهم الموقف", "topic": "المساعدة"},
                short_prefix="podcast-short",
            )
            self.assertIn("على مهل", published["title"])
            self.assertIn("#على_مهل", published["hashtags"])
            self.assertNotIn("#نداء_اليقظة", published["hashtags"])
            self.assertNotIn("#خارج_النص", published["hashtags"])
            self.assertNotIn("خارج النص", published["title"])
            self.assertNotIn("نداء اليقظة", published["youtube_tags"])
            self.assertIn("Ala Mahl", release._release_title(
                "podcast", "فهم الموقف", ala_mahl_podcast=True
            ))
            (root / "production.json").write_text(json.dumps({
                "format": "podcast", "template": "dialogue_qa"
            }, ensure_ascii=False), encoding="utf-8")
            legacy = release._derived_short_publish_metadata(
                root, parent_kind="podcast",
                parent={"title": "فهم الموقف", "topic": "المساعدة"},
                short_prefix="podcast-short",
            )
            self.assertIn("#خارج_النص", legacy["hashtags"])
            self.assertIn("#نداء_اليقظة", legacy["hashtags"])
            self.assertNotIn("#على_مهل", legacy["hashtags"])

    def test_podcast_modes_are_not_interchanged(self):
        self.assertEqual(_voice_performance_mode_for_brief({"format": "podcast"}),
                         "podcast_listener_proxy_qa")
        self.assertEqual(_voice_performance_mode_for_brief(
            {"format": "podcast"}, ready_template="ala_mahl_dialogue"),
            "ala_mahl_podcast_dialogue")
        self.assertEqual(_voice_performance_mode_for_brief({"format": "short"},
                         {"owner_authored": True, "short_template": "micro_story"},
                         ready_template="ala_mahl"), "micro_story")

    def test_a_iapetus_b_charon_but_legacy_a_orus(self):
        events = []

        def fake(_key, _text, path, **kwargs):
            events.append(kwargs)
            return _wav(Path(path))

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"CLEAN_V2_TTS_CACHE_PATH": ""}, clear=False
        ), patch("clean_v2.media._gemini38_synthesize", side_effect=fake):
            synth = media.GeminiOnlyVoiceSynthesizer("test-api-key")
            dialogue = "A: أهذا قرار عادل؟ B: يعتمد على ما نعرفه."
            synth.synthesize(dialogue, Path(tmp) / "ala.wav",
                             performance_mode="ala_mahl_podcast_dialogue")
            self.assertEqual(events[-1]["questioner_voice"], "Iapetus")
            self.assertEqual(events[-1]["primary_voice"], "Charon")
            self.assertEqual(synth.voice_roles["questioner"], "Iapetus")
            self.assertEqual(synth.voice_roles["responder"], "Charon")
            self.assertEqual(synth.voice_reference_profile,
                             media.GEMINI38_ALA_MAHL_REFERENCE_PROFILE)
            synth.synthesize(dialogue, Path(tmp) / "legacy.wav",
                             performance_mode="podcast_listener_proxy_qa")
            self.assertEqual(events[-1]["questioner_voice"], "Orus")
            self.assertEqual(events[-1]["primary_voice"], "Charon")
            self.assertEqual(synth.voice_roles["questioner"], "Orus")

    def test_new_duo_never_falls_back_to_unapproved_lite(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = media.GeminiOnlyVoiceSynthesizer(
                "test-api-key", tts_model="gemini-3.8-flash-lite-tts")
            with self.assertRaisesRegex(RuntimeError, "requires Flash TTS"):
                synth.synthesize("A: لماذا؟ B: لنتحدث.", Path(tmp) / "x.wav",
                                 performance_mode="ala_mahl_podcast_dialogue")

    def test_actual_sectioned_voice_uses_new_duo_but_prayer_keeps_B(self):
        from unittest.mock import patch as monkeypatch
        from clean_v2.pipeline import _synthesize_sectioned_voice
        from clean_v2.identity_sequence import inject_spoken_identity, PRAYER_SENTENCE

        sections = [
            {"id": "s1", "narration": "A: لماذا رفض طلب صديقه؟ B: ربما كان مضطرًا لذلك."},
            {"id": "s2", "narration": "A: لكن كيف نفسر رفضه؟ B: نحتاج إلى سماع تفاصيل الموقف. هذا هو الدرس."},
        ]
        inject_spoken_identity(sections, fmt="podcast")
        invocations = []

        def voice_stub(_api_key, words, path, **kwargs):
            invocations.append((words, kwargs["questioner_voice"], kwargs["primary_voice"],
                                kwargs.get("performance_mode")))
            return _wav(Path(path))

        def concat_stub(_parts, target, *args, **kwargs):
            return _wav(Path(target))

        with tempfile.TemporaryDirectory() as tmp, monkeypatch(
            "clean_v2.media._gemini38_synthesize", side_effect=voice_stub
        ), monkeypatch("clean_v2.pipeline.concat_wav_parts", side_effect=concat_stub), \
             monkeypatch.dict(os.environ, {"CLEAN_V2_TTS_CACHE_PATH": ""}, clear=False):
            folder = Path(tmp)
            (folder / "production.json").write_text(
                json.dumps({"template": "ala_mahl_dialogue"}), encoding="utf-8"
            )
            _synthesize_sectioned_voice(
                media.GeminiOnlyVoiceSynthesizer("test-key"), sections,
                folder / "narration.wav", fmt="podcast", require_charon_only=True,
                performance_mode="ala_mahl_podcast_dialogue",
            )

        hook = next(c for c in invocations if c[0].startswith("A:"))
        self.assertEqual((hook[1], hook[2], hook[3]),
                         ("Iapetus", "Charon", "ala_mahl_podcast_dialogue"))
        prayer = [c for c in invocations if PRAYER_SENTENCE in c[0]]
        self.assertEqual(len(prayer), 1)
        # Spoken prayer stays in existing Charon route, never replaced by Iapetus.
        self.assertEqual((prayer[0][1], prayer[0][2]), ("Orus", "Charon"))
        self.assertNotEqual(prayer[0][3], "ala_mahl_podcast_dialogue")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "FFmpeg and ffprobe required for real identity render")
    def test_real_ala_mahl_podcast_identity_media_is_independent(self):
        # No TTS, provider, stock search or licensed music is called.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = identity.identity_asset_paths(
                "podcast", template="ala_mahl_dialogue", runtime_dir=root
            )
            for kind, duration in (("intro", 6.0), ("outro", 6.5)):
                asset = assets[kind]
                self.assertTrue(asset.is_file())
                self.assertGreater(asset.stat().st_size, 1024)
                run = subprocess.run([
                    "ffprobe", "-v", "error", "-show_entries",
                    "format=duration:stream=codec_type,width,height",
                    "-of", "json", str(asset),
                ], text=True, capture_output=True, check=True, timeout=25)
                details = json.loads(run.stdout)
                self.assertAlmostEqual(float(details["format"]["duration"]), duration, delta=0.15)
                streams = details["streams"]
                video = next(row for row in streams if row.get("codec_type") == "video")
                self.assertIn((video["width"], video["height"]), {(1280, 720), (1920, 1080)})
                self.assertTrue(any(row.get("codec_type") == "audio" for row in streams))
            # Correct per-program identity files must never alias the V8 file names.
            for kind in ("intro", "outro"):
                self.assertNotIn("podcast_intro_v8", assets[kind].name)
                self.assertNotIn("podcast_outro_v8", assets[kind].name)
                self.assertTrue(
                    assets[kind].name.startswith("ala_mahl_") or assets[kind].name == f"{kind}_16x9.mp4"
                )

    def test_exact_owner_assets_override_only_new_podcast_identity(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            own = root / "approved"
            own.mkdir()
            for kind in ("intro", "prayer", "outro"):
                Image.new("RGB", (1920, 1080), (244, 234, 212)).save(
                    own / f"{kind}_16x9.png"
                )
            with patch.object(identity, "_ALA_MAHL_APPROVED_DIR", own), patch.object(
                identity, "_ensure_ala_mahl_podcast_assets",
                return_value=(root / "intro.mp4", root / "outro.mp4")
            ), patch.object(identity, "_ensure_podcast_v8_assets",
                return_value=(root / "legacy_intro.mp4", root / "legacy_outro.mp4")
            ):
                modern = identity.identity_asset_paths(
                    "podcast", template="ala_mahl_dialogue", runtime_dir=root
                )
                legacy = identity.identity_asset_paths(
                    "podcast", template="dialogue_qa", runtime_dir=root
                )
                self.assertEqual(modern["prayer"], own / "prayer_16x9.png")
                self.assertEqual(legacy["prayer"], identity._PRAYER_IMAGE)
                self.assertEqual(modern["intro"], root / "intro.mp4")
                self.assertEqual(legacy["intro"], root / "legacy_intro.mp4")

    def test_owner_asset_validation_rejects_invalid_resolution(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            own = Path(temporary)
            wrong = own / "intro_16x9.png"
            Image.new("RGB", (640, 480)).save(wrong)
            with patch.object(identity, "_ALA_MAHL_APPROVED_DIR", own):
                with self.assertRaisesRegex(RuntimeError, "approved_identity_invalid"):
                    identity._approved_ala_mahl_image("intro")
                self.assertIsNone(identity._approved_ala_mahl_image("outro"))

    def test_owner_image_digest_rotates_only_new_identity_video_cache(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved"
            approved.mkdir()
            for kind in ("intro", "outro"):
                Image.new("RGB", (1920, 1080), (244, 234, 212)).save(
                    approved / f"{kind}_16x9.png"
                )
            generated = []
            def fake_render(*, card, destination, duration, kind):
                generated.append((kind, card, duration))
                destination.write_bytes(b"test-video" * 300)
                return destination
            with patch.object(identity, "_ALA_MAHL_APPROVED_DIR", approved), patch.object(
                identity, "_render_podcast_v8_asset", side_effect=fake_render
            ):
                first = identity._ensure_ala_mahl_podcast_assets(root)
                again = identity._ensure_ala_mahl_podcast_assets(root)
                self.assertEqual(first, again)
                self.assertEqual(len(generated), 2)
                Image.new("RGB", (1920, 1080), (241, 229, 201)).save(
                    approved / "intro_16x9.png"
                )
                revised = identity._ensure_ala_mahl_podcast_assets(root)
                self.assertNotEqual(first[0], revised[0])
                self.assertEqual(first[1], revised[1])
                self.assertEqual(len(generated), 3)

    def test_podcast_identity_separate_old_path_untouched(self):
        ala_assets = (Path("ala-intro.mp4"), Path("ala-outro.mp4"))
        legacy_assets = (Path("old-intro.mp4"), Path("old-outro.mp4"))
        with patch.object(identity, "_ensure_ala_mahl_podcast_assets",
                          return_value=ala_assets) as ala, patch.object(
                          identity, "_ensure_podcast_v8_assets",
                          return_value=legacy_assets) as legacy:
            self.assertEqual(identity.identity_asset_paths(
                "podcast", template="ala_mahl_dialogue")["intro"], ala_assets[0])
            self.assertEqual(identity.identity_asset_paths(
                "podcast", template="dialogue_qa")["intro"], legacy_assets[0])
            self.assertEqual(ala.call_count, 1)
            self.assertEqual(legacy.call_count, 1)

    def test_approved_animated_identity_video_overrides_stills_and_prototype(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved"
            approved.mkdir()
            for kind in ("intro", "outro"):
                Image.new("RGB", (1920, 1080), (244, 234, 212)).save(approved / f"{kind}_16x9.png")
            intro = _identity_video(approved / "intro_16x9.mp4", seconds=6.0, size="1280x720")
            outro = _identity_video(approved / "outro_16x9.mp4", seconds=6.5, size="1920x1080")
            with patch.object(identity, "_ALA_MAHL_APPROVED_DIR", approved), patch.object(
                identity, "_render_podcast_v8_asset"
            ) as render:
                first = identity._ensure_ala_mahl_podcast_assets(root / "runtime")
                again = identity._ensure_ala_mahl_podcast_assets(root / "runtime")
            self.assertEqual(first, (intro, outro))
            self.assertEqual(first, again)
            render.assert_not_called()

    def test_approved_video_for_one_kind_leaves_the_other_on_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved = root / "approved"
            intro = _identity_video(approved / "intro_16x9.mp4", seconds=6.0)
            calls = []
            def fake_render(*, card, destination, duration, kind):
                calls.append(kind)
                destination.write_bytes(b"prototype-video" * 200)
                return destination
            with patch.object(identity, "_ALA_MAHL_APPROVED_DIR", approved), patch.object(
                identity, "_render_podcast_v8_asset", side_effect=fake_render
            ):
                got_intro, got_outro = identity._ensure_ala_mahl_podcast_assets(root / "runtime")
            self.assertEqual(got_intro, intro)
            self.assertEqual(calls, ["outro"])
            self.assertIn("prototype_v1", got_outro.name)

    def test_malformed_approved_video_fails_loudly_never_falls_back(self):
        cases = {
            "wrong_duration": dict(seconds=3.0),
            "wrong_resolution": dict(seconds=6.0, size="640x360"),
            "no_audio": dict(seconds=6.0, audio=False),
        }
        for name, options in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                approved = Path(temporary) / "approved"
                _identity_video(approved / "intro_16x9.mp4", **options)
                with patch.object(identity, "_ALA_MAHL_APPROVED_DIR", approved):
                    with self.assertRaisesRegex(RuntimeError, "approved_identity_video_invalid:intro"):
                        identity._approved_ala_mahl_video("intro")
                    with self.assertRaisesRegex(RuntimeError, "approved_identity_video_invalid:intro"):
                        identity._ensure_ala_mahl_podcast_assets(Path(temporary) / "runtime")
        with tempfile.TemporaryDirectory() as temporary:
            approved = Path(temporary) / "approved"
            approved.mkdir()
            (approved / "outro_16x9.mp4").write_bytes(b"not a video" * 200)
            with patch.object(identity, "_ALA_MAHL_APPROVED_DIR", approved):
                with self.assertRaisesRegex(RuntimeError, "approved_identity_video_invalid:outro"):
                    identity._approved_ala_mahl_video("outro")
                self.assertIsNone(identity._approved_ala_mahl_video("intro"))
                with self.assertRaises(ValueError):
                    identity._approved_ala_mahl_video("prayer")

    def test_installed_ala_mahl_identity_videos_are_valid_and_podcast_only(self):
        # The real files shipped in the repo, probed by the production validator.
        for kind in ("intro", "outro"):
            path = identity._approved_ala_mahl_video(kind)
            self.assertIsNotNone(path, f"{kind}_16x9.mp4 must be installed")
            self.assertTrue(path.is_file())
        with tempfile.TemporaryDirectory() as temporary:
            legacy = identity.identity_asset_paths(
                "podcast", template="dialogue_qa", runtime_dir=Path(temporary)
            )
            self.assertNotIn("ala_mahl_podcast_approved", str(legacy["intro"]))
            self.assertNotIn("ala_mahl_podcast_approved", str(legacy["outro"]))
            ala = identity.identity_asset_paths(
                "podcast", template="ala_mahl_dialogue", runtime_dir=Path(temporary)
            )
            self.assertEqual(ala["intro"].name, "intro_16x9.mp4")
            self.assertEqual(ala["outro"].name, "outro_16x9.mp4")


if __name__ == "__main__":
    unittest.main()
