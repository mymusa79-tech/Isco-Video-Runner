from __future__ import annotations

import unittest
from pathlib import Path


class F24NoProductionDispatchTests(unittest.TestCase):
    def test_f24_documentation_keeps_publication_manual(self) -> None:
        text = Path("docs/f24-final-master-acceptance-v2.md").read_text(encoding="utf-8")
        self.assertIn("Publication remains manual", text)
        self.assertIn("does not authorize a Production Run", text)


if __name__ == "__main__":
    unittest.main()
