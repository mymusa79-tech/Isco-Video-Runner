from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


class RetiredVoiceAbsenceTests(unittest.TestCase):
    def test_retired_voice_is_absent_from_every_tracked_runtime_file(self) -> None:
        retired = (
            (b"na" + b"bra").lower(),
            (b"pi" + b"per").lower(),
            (b"gemini-3." + b"1-flash-tts-preview").lower(),
            (b"azure_" + b"speech").lower(),
        )
        tracked = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
        offenders: dict[str, list[str]] = {}
        runtime_suffixes = {".py", ".yml", ".yaml", ".json", ".toml", ".sh", ".txt", ".lock"}
        for raw in tracked:
            if not raw:
                continue
            path = Path(os.fsdecode(raw))
            if not path.is_file() or path.suffix.lower() not in runtime_suffixes:
                continue
            try:
                data = path.read_bytes().lower()
            except OSError:
                continue
            hits = [needle.decode("ascii", "ignore") for needle in retired if needle in data]
            if hits:
                offenders[path.as_posix()] = hits
        self.assertEqual(
            offenders,
            {},
            f"retired voice references remain in runtime/test code: {offenders}",
        )

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
