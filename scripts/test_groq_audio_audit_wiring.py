from __future__ import annotations

import ast
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("run_v3_voice.py")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class GroqAudioAuditProductionWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.text)
        cls.main = next(
            node
            for node in cls.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )

    def test_g1_g2_runs_once_after_gold_and_before_manifest(self) -> None:
        calls = [
            (node.lineno, _call_name(node))
            for node in ast.walk(self.main)
            if isinstance(node, ast.Call)
        ]
        gold = [line for line, name in calls if name == "run_gold_enforce_phase4"]
        audio = [line for line, name in calls if name == "run_groq_audio_audit"]
        manifest = [line for line, name in calls if name == "_write_production_manifest"]
        self.assertEqual(len(gold), 1)
        self.assertEqual(len(audio), 1)
        self.assertEqual(len(manifest), 1)
        self.assertLess(gold[0], audio[0])
        self.assertLess(audio[0], manifest[0])

    def test_audit_remains_observe_only_and_defensive(self) -> None:
        self.assertIn("production unchanged", self.text)
        self.assertIn('groq = (os.environ.get("GROQ_API_KEY") or "").strip()', self.text)
        self.assertNotIn('secret("GROQ_API_KEY")', self.text)

    def test_audio_audit_is_embedded_in_durable_telemetry(self) -> None:
        self.assertIn('output_dir / "audio-transcript-audit.json"', self.text)
        self.assertIn('data["groq_audio_audit"] = audio', self.text)


if __name__ == "__main__":
    unittest.main()
