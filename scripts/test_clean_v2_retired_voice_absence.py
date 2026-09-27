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


if __name__ == "__main__":
    unittest.main()
