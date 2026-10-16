from __future__ import annotations

import copy
import io
import json
import stat
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image

from clean_v2 import owner_art, owner_pack, production_package as package, ready_production
from clean_v2.contracts import atomic_write_json
from clean_v2.identity_sequence import PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION
from scripts import telegram_clean_v2_control as control


def manifest(fmt="short", template=None):
    count = 2 if fmt == "podcast" else (5 if fmt == "film" else 3)
    texts = [
        "لم يكن يحتاج إلى يوم مثالي كي يبدأ. جلس أمام فنجانه البارد، ثم فتح النافذة وقرر أن يمنح خطوة واحدة فرصة حقيقية اليوم.",
        "في البداية بدت الخطوة أصغر من أن تصنع فرقًا. لكنه أكملها بهدوء، ورأى للمرة الأولى عملًا صغيرًا ينتهي بدل قائمة طويلة تنتظره.",
        "حين عاد إلى فنجانه كان يومه قد تغيّر قليلًا. ابتسم للباب المفتوح، وعرف أن الطريق يبدأ حين نعطي أول خطوة وقتها، ثم نواصل.",
        "بعد ذلك عاد إلى العمل نفسه، وهذه المرة عرف كيف يبدأ. لم يبحث عن خطة جديدة بل أكمل ما كان أمامه حتى النهاية.",
        "وفي المساء رأى ما أنجزه، فكتب لنفسه موعدًا بسيطًا للغد. كان التقدم واضحًا في فعل صغير، لا في انتظار شعور كبير كي يتحرك.",
    ]
    sections = [{"id": f"s{i+1}", "text": texts[i]} for i in range(count)]
    if fmt == "podcast":
        sections = [{"id": f"s{i+1}", "turns": [
            {"speaker": "A", "text": f"لماذا تبدو الخطوة {i+1} صعبة أحيانًا؟"},
            {"speaker": "B", "text": texts[i] + " " + texts[i+2]},
        ]} for i in range(count)]
    doc = {"schema_version": 1, "format": fmt,
           "template": template or {"short": "micro_story", "film": "direct_cinematic", "podcast": "dialogue_qa"}[fmt],
           "title": "خطوة واحدة تغيّر يومك", "approval": {"status": "approved", "reviewer": "Claude"},
           "script": {"sections": sections}, "performance": {"direction": "حديث طبيعي مع صديق، بوقفات قصيرة."},
           "scenes": [{"id": f"image_{i+1}", "section_id": s["id"], "asset": f"assets/scene_{i+1:02d}.png",
                       "start_text": package.spoken_text(package.section_text(s)).split(". ")[0],
                       "motion": "slow_pan" if i % 2 else "slow_zoom", "transition": "dissolve"}
                      for i, s in enumerate(sections)],
           "editing": {"music": "none"}, "captions": {"font": "Cairo Bold", "max_lines": 2, "enabled": True},
           "cover": {"asset": None, "text": "خطوة واحدة"},
           "publishing": {"title": "خطوة واحدة تغيّر يومك", "description": "قصة عن بداية صغيرة.", "hashtags": ["#نداء_اليقظة"]}}
    if fmt == "podcast":
        doc["roles"] = {"A": "listener", "B": "host"}
    return doc


def archive_bytes(doc=None, *, missing=None, extra=None, size=(180, 320)):
    doc = doc or manifest()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("production.json", json.dumps(doc, ensure_ascii=False))
        for i, scene in enumerate(doc["scenes"]):
            if scene["asset"] == missing:
                continue
            picture = Image.new("RGB", size, (20 + i * 25, 30, 50))
            picture.paste((180, 120, 60), (20+i*10, 45, 80+i*10, 200))
            raw = io.BytesIO()
            picture.save(raw, "PNG")
            archive.writestr(scene["asset"], raw.getvalue())
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return stream.getvalue()


def imported_pack(doc=None):
    checked = package.inspect_archive(archive_bytes(doc))
    doc = checked["manifest"]
    return {"schema_version": 1, "format": doc["format"], "title": doc["title"], "production": doc,
            "asset_sha256": checked["asset_sha256"], "zip_sha256": checked["zip_sha256"]}


class ImportTests(unittest.TestCase):
    def test_valid_zip_requires_one_explicit_confirmation_and_no_scene_picker(self):
        data = archive_bytes()
        update = {"message": {"document": {"file_id": "approved", "file_name": "ready.zip", "file_size": len(data)}}}
        state = control.default_state()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(control, "authorized", return_value=True), \
             mock.patch.object(control, "_download_telegram_file", return_value=data), mock.patch.object(control, "send_telegram") as send:
            dispatch = Path(temporary) / "dispatch.json"
            control.handle_update(state, update, dispatch)
            request = state["requests"][state["current_request_id"]]
            self.assertEqual(request["status"], "awaiting_confirmation")
            self.assertFalse(dispatch.exists())
            self.assertIn("لم يبدأ الإنتاج", send.call_args.args[0])
            control._stage_confirmation_dispatch(state, dispatch)
            self.assertTrue(dispatch.is_file())
            self.assertNotIn("scene_review", request)
            self.assertEqual(request["owner_pack"]["production"], manifest())

    def test_corrupt_zip_missing_json_images_and_bad_json_are_rejected(self):
        for data in (b"not a zip", archive_bytes(missing="assets/scene_02.png"),
                     archive_bytes(extra={"unknown.exe": b"code"}), b""):
            with self.subTest(data=data[:4]), self.assertRaises(owner_pack.OwnerPackError):
                package.inspect_archive(data)
        for raw in ('{"schema_version":1,"schema_version":2}', '{"x": NaN}', '[]'):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("production.json", raw)
            with self.assertRaises(owner_pack.OwnerPackError):
                package.inspect_archive(stream.getvalue())

    def test_zip_slip_absolute_backslash_duplicate_symlink_and_bombs_are_rejected(self):
        for name in ("../escape.jpg", "/tmp/escape.png", "assets/../../escape.png", "assets\\escape.png", "C:/escape.png", "assets/a.svg"):
            with self.subTest(name=name), self.assertRaises(owner_pack.OwnerPackError):
                package.inspect_archive(archive_bytes(extra={name: b"bad"}))
        for variant in ("duplicate", "symlink", "bomb"):
            stream = io.BytesIO(archive_bytes())
            with zipfile.ZipFile(stream, "a") as archive:
                if variant == "symlink":
                    info = zipfile.ZipInfo("assets/link.png")
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, "../../secret")
                elif variant == "duplicate":
                    archive.writestr("PRODUCTION.JSON", "{}")
                else:
                    archive.writestr("assets/bomb.png", b"0" * 1024 * 1024, compress_type=zipfile.ZIP_DEFLATED)
            with self.subTest(variant=variant), self.assertRaises(owner_pack.OwnerPackError):
                package.inspect_archive(stream.getvalue())
        with mock.patch.object(package, "MAX_ZIP_BYTES", 10), self.assertRaises(owner_pack.OwnerPackError):
            package.inspect_archive(archive_bytes())

    def test_bad_image_wrong_aspect_and_missing_cover_are_rejected(self):
        with self.assertRaises(owner_pack.OwnerPackError):
            package.inspect_archive(archive_bytes(extra={"assets/not_image.png": b"not image"}))
        doc = manifest(template="ala_mahl")
        doc["cover"]["asset"] = "assets/cover.png"
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "مفقودة"):
            package.inspect_archive(archive_bytes(doc))
        doc = manifest(template="ala_mahl")
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "9:16"):
            package.inspect_archive(archive_bytes(doc, size=(320, 180)))
        data = archive_bytes(doc)
        checked = package.inspect_archive(data)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(owner_pack.OwnerPackError, "تغير"):
                package.extract_archive(data, root, expected_sha256="0" * 64)
            package.extract_archive(data, root, expected_sha256=checked["zip_sha256"])
            with self.assertRaisesRegex(owner_pack.OwnerPackError, "فارغ"):
                package.extract_archive(data, root, expected_sha256=checked["zip_sha256"])

    def test_unsafe_json_voice_override_reordered_scenes_and_A_B_drift_are_rejected(self):
        for mutate in (lambda d: d.update(voice="other"), lambda d: d["scenes"].reverse(),
                       lambda d: d["scenes"][0].update(asset="../a.png")):
            doc = manifest(); mutate(doc)
            with self.assertRaises(owner_pack.OwnerPackError):
                package.validate_manifest(doc)
        doc = manifest("podcast"); doc["roles"]["B"] = "listener"
        with self.assertRaises(owner_pack.OwnerPackError):
            package.validate_manifest(doc)


class ApprovedArtifactsTests(unittest.TestCase):
    def test_documented_example_matches_the_real_validator(self):
        example = Path(__file__).resolve().parents[1] / "docs/examples/production.ala_mahl.json"
        package.validate_manifest(json.loads(example.read_text()))

    def test_performance_is_metadata_and_keeps_A_B_voice_bindings(self):
        from clean_v2.media import _gemini38_synthesize, GEMINI38_TTS_MODEL
        from clean_v2.tts_cache import build_binding
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"output":[]}'
        with tempfile.TemporaryDirectory() as temporary, mock.patch("clean_v2.media.urllib.request.urlopen", return_value=response) as send:
            with self.assertRaises(RuntimeError):
                _gemini38_synthesize("fake-test-key", "A: لماذا نؤجل؟ B: لأن البداية تبدو كبيرة.", Path(temporary)/"unused.wav",
                                     model=GEMINI38_TTS_MODEL, primary_voice="Charon", questioner_voice="Orus",
                                     performance_direction={"direction": "بهدوء", "A": "فضول", "B": "وضوح"})
            payload = json.loads(send.call_args.args[0].data)
        parts = payload["input"][0]["content"]
        self.assertEqual([p["text"] for p in parts], ["لماذا نؤجل؟", "لأن البداية تبدو كبيرة."])
        self.assertEqual(payload["generation_config"]["speech_config"]["speakers"],
                         [{"speaker": "A", "voice": "Orus"}, {"speaker": "B", "voice": "Charon"}])
        self.assertIn("فضول", parts[0]["annotations"][0]["style"])
        base = dict(transcript="نفس الكلمات", model=GEMINI38_TTS_MODEL, primary_voice="Charon", questioner_voice="Orus", performance_mode="", provider="test")
        self.assertNotEqual(build_binding(**base), build_binding(**base, performance_direction={"direction": "بهدوء"}))

    def test_all_formats_build_without_writing_or_relocating_any_approved_text(self):
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as temporary:
                pack = imported_pack(manifest(fmt)); root = Path(temporary)
                brief = {"format": fmt}
                script = owner_pack.build_artifacts(pack, brief, root)
                approved = copy.deepcopy(script)
                self.assertEqual(json.loads((root / "production.json").read_text()), pack["production"])
                joined = " ".join(s["narration"] for s in script["sections"])
                self.assertEqual(joined.count(PRAYER_SENTENCE), 1)
                from clean_v2.identity_sequence import LONG_CHANNEL_DEFINITION, PODCAST_FALLBACK_BRIDGE
                identity_report = json.loads((root / "narrative-identity.json").read_text())
                # Full CI loads the pinned Engine's actual Long opener/closer;
                # standalone local tests may use the default fallback identity.
                for identity in (PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION, LONG_CHANNEL_DEFINITION,
                                 PODCAST_FALLBACK_BRIDGE, identity_report["opener"], identity_report["closer"]):
                    if identity:
                        joined = joined.replace(identity, "")
                joined = package.spoken_text(joined)
                for section in pack["production"]["script"]["sections"]:
                    for text in [section["text"]] if "text" in section else [t["text"] for t in section["turns"]]:
                        self.assertIn(" ".join(text.split()), joined)
                self.assertEqual(joined, package.spoken_text(" ".join(
                    package.section_text(s) for s in pack["production"]["script"]["sections"])))
                self.assertEqual(script, approved)
                self.assertEqual(json.loads((root / "cta-plan.json").read_text())["mode"], "none")

    def test_ala_mahl_is_short_with_ordered_original_images_and_no_voice_definition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); pack = imported_pack(manifest(template="ala_mahl"))
            script = owner_pack.build_artifacts(pack, {"format": "short"}, root)
            self.assertNotIn(SHORT_CHANNEL_DEFINITION, " ".join(s["narration"] for s in script["sections"]))
            story = json.loads((root / "visual-story.json").read_text())
            self.assertEqual([b["owner_asset"] for b in story["beats"]],
                             ["assets/scene_01.png", "assets/scene_01.png", "assets/scene_02.png", "assets/scene_03.png"])

    def test_missing_approved_image_never_calls_stock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            doc = manifest(template="ala_mahl")
            atomic_write_json(root / "production.json", doc)
            inner = mock.Mock()
            source = owner_art.OwnerArtVisualSource(inner, root)
            with self.assertRaises(owner_pack.OwnerPackError):
                source.acquire({"visual_story": ready_production.build_story(doc)}, root / "out", "short", 5)
            inner.acquire.assert_not_called()

    def test_approved_music_none_is_offline_and_does_not_change_audio(self):
        from clean_v2.short_audio_polish import apply_topic_audio_polish
        with tempfile.TemporaryDirectory() as temporary, mock.patch("clean_v2.short_audio_polish.select_music_track") as music:
            root = Path(temporary); atomic_write_json(root / "production.json", manifest())
            report = apply_topic_audio_polish(output_dir=root, final_path=root / "unused.mp4", narration_path=root / "unused.wav", script={}, fmt="short")
            music.assert_not_called()
            self.assertFalse(report["music_added"])


if __name__ == "__main__":
    unittest.main()
