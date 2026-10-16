"""Local media integration: synthetic WAV only, real image motion/render/captions."""
from __future__ import annotations

import array
import json
import math
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from clean_v2 import owner_art, owner_pack, production_package
from clean_v2.contracts import atomic_write_json, compute_brief_sha256
from clean_v2.media import inspect_final
from clean_v2.pipeline import CleanV2Pipeline
from clean_v2.short_timed_text import apply_short_timed_text
from scripts.test_clean_v2 import _passing_audio_mastering, _passing_final_master_qc, _passing_visual_qa
from scripts.test_production_package import archive_bytes, imported_pack, manifest


class LocalVoice:
    last_provider = "gemini-3.8:Charon"
    fallback_used = False
    charon_attempts = 1

    def __init__(self):
        self.spoken = []

    def synthesize(self, text, path, **kwargs):
        self.spoken.append((text, kwargs))
        seconds = max(0.8, len(text.split()) * 0.38)
        rate = 24000
        samples = array.array("h", (round(1500 * math.sin(i * 2 * math.pi * 220 / rate)) for i in range(round(seconds * rate))))
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(rate)
            audio.writeframes(samples.tobytes())
        return path


class ReadyProductionMediaTests(unittest.TestCase):
    def test_ala_mahl_multiple_approved_images_real_render_without_network(self):
        doc = manifest(template="ala_mahl")
        pack = imported_pack(doc)
        brief = {"approved_by_user": True, "approved_topic": doc["title"], "format": "short", "language": "ar",
                 "audience": "Arabic-speaking adults", "editorial_intent": "نص معتمد", "research_pack": [],
                 "hard_constraints": ["No fabricated facts."]}
        voice = LocalVoice()

        def cinematic(**kwargs):
            captions = apply_short_timed_text(output_dir=kwargs["output_dir"], final_path=kwargs["final_path"],
                                               narration_path=kwargs["narration_path"], script=kwargs["script"])
            atomic_write_json(kwargs["output_dir"] / "short-timed-text.json", captions)
            return {"status": "pass", "ai_calls_added": 0}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); art = root / "art"; output = root / "out"
            data = archive_bytes(doc)
            production_package.extract_archive(data, art, expected_sha256=pack["zip_sha256"])
            atomic_write_json(root / "pack.json", pack); atomic_write_json(root / "brief.json", brief)
            stock = mock.Mock(events=[]); router = mock.Mock(events=[])
            router.route.side_effect = AssertionError("approved script must never call a planning/writing model")
            with mock.patch.dict(os.environ, {owner_pack.OWNER_PACK_ENV: str(root / "pack.json"), owner_art.ART_DIR_ENV: str(art),
                                              "CLEAN_V2_SCENE_REVIEW": "", "CLEAN_V2_RUN_MODE": "simple"}), \
                 mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network in this integration test")):
                result = CleanV2Pipeline(
                    router=router, voice_synthesizer=voice, visual_source=owner_art.OwnerArtVisualSource(stock, art),
                    visual_qa=_passing_visual_qa, opening_director=lambda **kw: {"status": "skipped_simple_mode", "slots": []},
                    cinematic_layer=cinematic, final_master_qc=_passing_final_master_qc, audio_mastering=_passing_audio_mastering,
                ).run(brief_path=root / "brief.json", approved_sha256=compute_brief_sha256(brief), output_dir=output,
                      engine_sha="a"*40, runner_sha="b"*40)
            self.assertEqual(result["status"], "pass")
            media = inspect_final(output / "final.mp4")
            self.assertEqual((media["width"], media["height"]), (1080, 1920))
            self.assertTrue(25 <= media["duration_seconds"] <= 60)
            timeline = json.loads((output / "timeline-first.json").read_text())
            kinds = [e["kind"] for e in timeline["identity_events"]]
            self.assertNotIn("channel_identity", kinds); self.assertNotIn("intro", kinds)
            self.assertTrue((output / ".ala-mahl-signature.png").is_file())
            self.assertGreaterEqual(len(voice.spoken), 5)
            self.assertEqual(json.loads((output / "plan.json").read_text())["short_template"], "ala_mahl")
            self.assertEqual(json.loads((output / "run-manifest.json").read_text())["short_template"], "ala_mahl")
            self.assertTrue(any(k.get("performance_direction") for _, k in voice.spoken))
            rights = json.loads((output / "rights-manifest.json").read_text())["assets"]
            self.assertEqual({r["asset_id"] for r in rights}, {s["asset"] for s in doc["scenes"]})
            self.assertTrue(all(r["provider"] == "owner_art" for r in rights))
            stock.acquire.assert_not_called(); router.route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
