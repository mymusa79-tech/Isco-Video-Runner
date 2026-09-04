from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ProfessionalLongFormatV4ContractTests(unittest.TestCase):
    def test_v4_runtime_request_is_derived_from_bound_brief_not_raw_auto_request(self) -> None:
        path = ROOT / "scripts" / "telegram_v4_ingress.py"
        text = path.read_text(encoding="utf-8")
        ast.parse(text, filename=str(path))
        materialize = text.index("materialize_approved_brief(request")
        bound_read = text.index("bound_brief = json.loads")
        resolved = text.index('fmt = str(bound_brief.get("format")')
        request_write = text.index('json.dumps({"topic": str(request.get("approved_topic") or "").strip(), "format": fmt}')
        consume = text.index("consume_dispatch_authorization(")
        self.assertLess(materialize, bound_read)
        self.assertLess(bound_read, resolved)
        self.assertLess(resolved, request_write)
        self.assertLess(request_write, consume)
        self.assertNotIn('else str(request.get("format") or "film")', text)


if __name__ == "__main__":
    unittest.main()
