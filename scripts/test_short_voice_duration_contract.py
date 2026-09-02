from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import short_voice_v2 as voice


class ShortVoiceDurationContractTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path, Path]:
        final = root / "final.mp4"
        narration = root / "voice.wav"
        output = root / "voiced.mp4"
        final.write_bytes(b"v" * 4096)
        narration.write_bytes(b"a" * 4096)
        return final, narration, output

    def test_no_audio_branch_pads_voice_and_preserves_source_visual_duration(self):
        with tempfile.TemporaryDirectory() as td:
            final, narration, output = self._paths(Path(td))
            seen = {}

            def fake_run(command, **_kwargs):
                seen["command"] = list(command)
                output.write_bytes(b"mixed" * 1024)
                return mock.Mock(returncode=0)

            with mock.patch.object(voice, "_has_audio", return_value=False), \
                    mock.patch.object(voice, "_final_duration", side_effect=[15.0, 15.0]), \
                    mock.patch.object(voice.subprocess, "run", side_effect=fake_run):
                result = voice._mix_voice(final, narration, output)

        self.assertEqual(result, output)
        command = seen["command"]
        self.assertNotIn("-shortest", command)
        self.assertIn("-t", command)
        self.assertEqual(command[command.index("-t") + 1], "15.000")
        filter_value = command[command.index("-filter_complex") + 1]
        self.assertIn("apad", filter_value)

    def test_mix_fails_before_replacement_if_duration_contract_drifts(self):
        with tempfile.TemporaryDirectory() as td:
            final, narration, output = self._paths(Path(td))

            def fake_run(_command, **_kwargs):
                output.write_bytes(b"mixed" * 1024)
                return mock.Mock(returncode=0)

            with mock.patch.object(voice, "_has_audio", return_value=False), \
                    mock.patch.object(voice, "_final_duration", side_effect=[15.0, 8.0]), \
                    mock.patch.object(voice.subprocess, "run", side_effect=fake_run):
                with self.assertRaisesRegex(RuntimeError, "changed approved visual duration"):
                    voice._mix_voice(final, narration, output)

    def test_existing_audio_mix_is_also_duration_checked(self):
        with tempfile.TemporaryDirectory() as td:
            final, narration, output = self._paths(Path(td))
            seen = {}

            def fake_run(command, **_kwargs):
                seen["command"] = list(command)
                output.write_bytes(b"mixed" * 1024)
                return mock.Mock(returncode=0)

            with mock.patch.object(voice, "_has_audio", return_value=True), \
                    mock.patch.object(voice, "_final_duration", side_effect=[15.0, 15.05]), \
                    mock.patch.object(voice.subprocess, "run", side_effect=fake_run):
                voice._mix_voice(final, narration, output)

        self.assertNotIn("-shortest", seen["command"])


if __name__ == "__main__":
    unittest.main()
