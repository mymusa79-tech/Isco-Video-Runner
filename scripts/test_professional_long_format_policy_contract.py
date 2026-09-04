from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ProfessionalLongFormatPolicyContractTests(unittest.TestCase):
    def test_policy_is_installed_after_final_telegram_wrappers(self) -> None:
        path = ROOT / "scripts" / "telegram_topic_research_v2.py"
        text = path.read_text(encoding="utf-8")
        ast.parse(text, filename=str(path))
        mission = text.index("operator_mission_control.install()")
        policy = text.index("long_format_policy.install(panel=core.panel)")
        main_call = text.index("core.panel.main()")
        self.assertLess(mission, policy)
        self.assertLess(policy, main_call)

    def test_policy_module_has_no_provider_or_network_dependency(self) -> None:
        text = (ROOT / "scripts" / "telegram_long_format_policy.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        imported = {
            node.names[0].name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import) and node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        forbidden = {"urllib", "requests", "httpx", "openai", "google.generativeai"}
        self.assertFalse(imported & forbidden)
        self.assertIn('extra_ai_calls": 0', text)


if __name__ == "__main__":
    unittest.main()
