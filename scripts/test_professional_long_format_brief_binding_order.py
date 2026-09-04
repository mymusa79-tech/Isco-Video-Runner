from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ProfessionalLongFormatBriefBindingOrderTests(unittest.TestCase):
    def test_resolution_precedes_approval_binding(self) -> None:
        path = ROOT / "scripts" / "control_approved_brief.py"
        text = path.read_text(encoding="utf-8")
        ast.parse(text, filename=str(path))
        resolve = text.index("fmt = resolve_control_format(request)")
        brief = text.index('"format": fmt')
        bind = text.index("bound = attach_approval_binding(brief)")
        self.assertLess(resolve, brief)
        self.assertLess(brief, bind)


if __name__ == "__main__":
    unittest.main()
