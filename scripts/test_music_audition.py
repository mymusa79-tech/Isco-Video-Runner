from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import music_library
from scripts import music_audition


class MusicAuditionTests(unittest.TestCase):
    def test_mixes_each_ready_track_at_the_production_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            voice = root / "run"
            voice.mkdir()
            tone = root / "tone.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=40",
                            "-c:a", "pcm_s16le", str(voice / "narration-mastered.wav")], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=60",
                            "-c:a", "pcm_s16le", str(tone)], check=True)
            fake = {"ready": [{"id": "a-little-faith", "title": "A", "path": str(tone)}], "unavailable": []}
            with mock.patch.object(music_library, "ensure_music_library", return_value=fake):
                rows = music_audition.build(voice, root / "out", start=2, duration=20, fmt="podcast")
            self.assertEqual([r["id"] for r in rows], ["a-little-faith"])
            self.assertAlmostEqual(rows[0]["relative_to_voice_db"], -24.0, delta=1.0)
            self.assertTrue((root / "out" / "0-voice-only.mp3").is_file())
            self.assertTrue((root / "out" / "1-a-little-faith.mp3").is_file())


if __name__ == "__main__":
    unittest.main()
