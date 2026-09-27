from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


class RetiredVoiceAbsenceTests(unittest.TestCase):
    def test_retired_voice_is_absent_from_every_tracked_file(self) -> None:
        needle = (b"na" + b"bra").lower()
        tracked = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
        offenders: list[str] = []
        for raw in tracked:
            if not raw:
                continue
            path = Path(os.fsdecode(raw))
            if not path.is_file():
                continue
            try:
                data = path.read_bytes().lower()
            except OSError:
                continue
            if needle in data:
                offenders.append(path.as_posix())
        self.assertEqual(offenders, [], f"retired voice references remain: {offenders}")

    def test_retired_local_voice_dependencies_are_absent_from_workflows(self) -> None:
        retired = (
            "espeak-ng",
            "libsndfile1",
            "kokoro",
            "camel-tools",
            "camel_data",
            "arabic_g2p.py",
            "phonemizer-fork",
            "misaki[en]",
            "piper-tts",
            "piper.download_voices",
            "azure_speech_key",
            "azure_speech_region",
        )
        workflow_root = Path(".github/workflows")
        offenders: dict[str, list[str]] = {}
        for path in sorted(workflow_root.glob("*.yml")):
            text = path.read_text(encoding="utf-8").lower()
            hits = [item for item in retired if item.lower() in text]
            if hits:
                offenders[path.as_posix()] = hits
        self.assertEqual(
            offenders,
            {},
            f"retired local voice dependencies remain in workflows: {offenders}",
        )



if __name__ == "__main__":
    unittest.main()
