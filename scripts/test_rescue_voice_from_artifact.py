from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from clean_v2 import pipeline
from scripts.rescue_voice_from_artifact import rescue

ENGINE = "e" * 40


def _build_run(root: Path) -> None:
    root.mkdir(parents=True)
    for name in ("brief.json", "plan.json", "production.json", "visual-story.json", "script.json",
                 "narrative-identity.json", "cta-plan.json", "audit-checkpoint.json",
                 "voice-sections.json", "rights-manifest.json"):
        (root / name).write_text(json.dumps({"n": name}), encoding="utf-8")
    (root / "rights-manifest.json").write_text(
        json.dumps({"assets": [{"local_file": "a.png"}]}), encoding="utf-8")
    (root / "narration.txt").write_text("نص\n", encoding="utf-8")
    (root / "narration.wav").write_bytes(b"RIFFwav")
    (root / "audio").mkdir()
    (root / "audio" / "01.wav").write_bytes(b"RIFFchunk")
    (root / "visuals").mkdir()
    (root / "visuals" / "a.png").write_bytes(b"png")
    (root / "audit-checkpoint.json").write_text(json.dumps({
        "schema_version": 1, "status": "pass",
        "script_sha256": hashlib.sha256((root / "script.json").read_bytes()).hexdigest(),
        "narration_sha256": hashlib.sha256((root / "narration.txt").read_bytes()).hexdigest(),
        "audit_result": {"status": "pass"},
    }), encoding="utf-8")
    staging = root / "owner-pack-staging"
    staging.mkdir()
    (staging / "cta-plan.json").write_bytes((root / "cta-plan.json").read_bytes())
    pipeline._write_resume_checkpoint(
        root, completed_stage="visuals", approved_brief_sha256="b" * 64, engine_sha=ENGINE,
        runner_sha="r" * 40, max_visuals=5, voice_provider="gemini-3.8:Charon",
        voice_fallback_used=False,
    )


class RescueVoiceTests(unittest.TestCase):
    def test_rebuilds_valid_voice_checkpoint_after_post_render_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "podcast"
            _build_run(root)
            (root / "cta-plan.json").write_text('{"mutated":1}', encoding="utf-8")
            (root / "rights-manifest.json").write_text('{"assets":[],"mutated":1}', encoding="utf-8")
            target = Path(tmp) / "cache" / "podcast"
            rescue(root, target)
            checkpoint = json.loads((target / "resume-checkpoint.json").read_text())
            self.assertEqual(checkpoint["completed_stage"], "voice")
            self.assertNotIn("rights-manifest.json", checkpoint["artifacts"])
            loaded = pipeline._load_resume_checkpoint(
                target, approved_brief_sha256="b" * 64, engine_sha=ENGINE,
                runner_sha="x" * 40, max_visuals=5, force_from_voice=True)
            self.assertIsNotNone(loaded)

    def test_refuses_voice_that_cannot_be_proven_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "podcast"
            _build_run(root)
            (root / "narration.wav").write_bytes(b"tampered")
            with self.assertRaises(RuntimeError):
                rescue(root, Path(tmp) / "cache" / "podcast")


if __name__ == "__main__":
    unittest.main()
