"""Local media integration: synthetic WAV only, real image motion/render/captions."""
from __future__ import annotations

import array
import hashlib
import json
import math
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from clean_v2 import owner_art, owner_pack, production_package
from clean_v2.contracts import atomic_write_json, compute_brief_sha256
from clean_v2.media import inspect_final
from clean_v2.identity_sequence import identity_asset_paths
from clean_v2.pipeline import CleanV2Pipeline
from clean_v2.short_timed_text import apply_short_timed_text
from scripts.test_clean_v2 import _passing_audio_mastering, _passing_final_master_qc, _passing_visual_qa
from scripts.test_production_package import ala_mahl_manifest, archive_bytes, imported_pack, manifest


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
    def test_two_and_four_images_real_render_without_network_preserve_the_same_voice(self):
        self.assertEqual(self._render_approved_images(2), self._render_approved_images(4))

    def test_nidaa_last_content_frame_keeps_approved_picture_until_outro(self):
        self._render_approved_images(3, template="micro_story")

    def _render_approved_images(self, count, template="ala_mahl"):
        ala_mahl = template == "ala_mahl"
        doc = ala_mahl_manifest(count) if ala_mahl else manifest(template=template)
        for scene in doc["scenes"]:
            scene["motion"] = "hold"
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
            before_assets = {s["asset"]: hashlib.sha256((art / s["asset"]).read_bytes()).hexdigest() for s in doc["scenes"]}
            atomic_write_json(root / "pack.json", pack); atomic_write_json(root / "brief.json", brief)
            stock = mock.Mock(events=[]); router = mock.Mock(events=[])
            router.route.side_effect = AssertionError("approved script must never call a planning/writing model")
            with mock.patch.dict(os.environ, {owner_pack.OWNER_PACK_ENV: str(root / "pack.json"), owner_art.ART_DIR_ENV: str(art),
                                              "CLEAN_V2_SCENE_REVIEW": "", "CLEAN_V2_SIMPLE_MODE": "true"}), \
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
            self.assertEqual("channel_identity" in kinds, not ala_mahl)
            self.assertNotIn("intro", kinds)
            self.assertFalse((output / ".ala-mahl-signature.png").exists())
            self.assertEqual(len(voice.spoken), 6 if ala_mahl else 7)
            authored = json.loads((output / "script.json").read_text())["sections"]
            self.assertEqual(" ".join(text for text, _ in voice.spoken),
                             " ".join(s["narration"] for s in authored))
            outro = next(e for e in timeline["identity_events"] if e["kind"] == "outro")
            spoken_units = [u for u in timeline["audio_units"] if u["provider"] != "deterministic_silence"]
            self.assertEqual(outro["start"], spoken_units[-1]["end"])
            self.assertEqual(timeline["audio_units"][-1]["role"], "final_silence")
            # The last audible sample belongs to the story; the outro may only
            # start after it, never over the last word or by shortening the voice.
            with wave.open(str(output / "narration-mastered.wav"), "rb") as audio:
                samples = array.array("h", audio.readframes(audio.getnframes()))
                sample_rate = audio.getframerate() * audio.getnchannels()
            last_audible = next(i for i in range(len(samples) - 1, -1, -1) if samples[i])
            self.assertGreaterEqual(outro["start"] + 0.001, (last_audible + 1) / sample_rate)
            self.assertAlmostEqual(outro["end"] - outro["start"], 67 / 30 if ala_mahl else 1.25, delta=0.01)
            self.assertEqual(json.loads((output / "identity-sequence.json").read_text())["sequence"][-1],
                             "ala_mahl_outro" if ala_mahl else "short_outro")
            # A late frame proves the full approved animation survives composition,
            # rather than being frozen at t=0 or cut at the old 1.25-second tail.
            def frame(path, at):
                return subprocess.run([
                    "ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
                    "-vf", "scale=180:320", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
                ], check=True, capture_output=True, timeout=30).stdout
            late = 2.05 if ala_mahl else 1.05
            expected = frame(identity_asset_paths("short", template=template)["outro"], late)
            actual = frame(output / "final.mp4", outro["start"] + late)
            self.assertEqual(len(actual), 180 * 320 * 3)
            self.assertEqual(len(actual), len(expected))
            self.assertLess(sum(abs(a - b) for a, b in zip(actual, expected)) / len(actual), 3.0)
            self.assertEqual(json.loads((output / "plan.json").read_text())["short_template"], template)
            self.assertEqual(json.loads((output / "run-manifest.json").read_text())["short_template"], template)
            self.assertTrue(any(k.get("performance_direction") for _, k in voice.spoken))
            rights = json.loads((output / "rights-manifest.json").read_text())["assets"]
            # The last content frame can fall before the exact outro start by a
            # fraction of a frame. It must retain the approved source, not slate.
            last_frame = (math.ceil(outro["start"] * 30) - 1) / 30
            expected = frame(output / "visuals" / rights[-1]["local_file"], 0.0)
            actual = frame(output / "final.mp4", last_frame - 0.00001)
            self.assertLess(sum(abs(a - b) for a, b in zip(actual[:180*60*3], expected[:180*60*3])) / (180*60*3), 3.0)
            self.assertEqual({r["asset_id"] for r in rights}, {s["asset"] for s in doc["scenes"]})
            self.assertTrue(all(r["provider"] == "owner_art" for r in rights))
            self.assertEqual(before_assets, {name: hashlib.sha256((art / name).read_bytes()).hexdigest() for name in before_assets})
            editing = json.loads((output / "edit-decision-contract.json").read_text())
            self.assertEqual(len(editing["slots"]), count + 1)  # prayer splits the first picture once
            self.assertTrue(all(r["owner_motion"] == "hold" for r in rights))
            stock.acquire.assert_not_called(); router.route.assert_not_called()
            return ([text for text, _kwargs in voice.spoken],
                    hashlib.sha256((output / "narration-mastered.wav").read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
