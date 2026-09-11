from __future__ import annotations

import ast
import unittest
from pathlib import Path

from scripts import quality_capability_router as router


_REPO_ROOT = Path(__file__).resolve().parent.parent

# These are quality *gate entrypoints*. Provider wire adapters are intentionally separate
# and may contain provider SDK/HTTP details, but these files must stay provider-neutral.
_QUALITY_GATE_ENTRYPOINTS = (
    "scripts/audio_producer_final_certificate.py",
    "scripts/final_master_qc.py",
    "scripts/gold_enforce_phase4.py",
)

_PROVIDER_TRANSPORT_MARKERS = (
    "api.openai.com",
    "api.groq.com",
    "openrouter.ai/api",
    "generativelanguage.googleapis.com",
    "google.genai",
    "from google import genai",
    "requests.post(",
    "requests.get(",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
)


class QualityArchitectureGuardTests(unittest.TestCase):
    def test_gate_entrypoints_contain_no_direct_provider_transport_or_secret_lookup(self) -> None:
        violations: list[str] = []
        for relative in _QUALITY_GATE_ENTRYPOINTS:
            path = _REPO_ROOT / relative
            source = path.read_text(encoding="utf-8")
            for marker in _PROVIDER_TRANSPORT_MARKERS:
                if marker in source:
                    violations.append(f"{relative}:{marker}")
        self.assertEqual(
            violations,
            [],
            "Release-critical quality gates must call capability routing/adapters, not providers directly",
        )

    def test_audio_final_gate_is_bound_to_capability_router_adapter(self) -> None:
        source = (_REPO_ROOT / "scripts/audio_producer_final_certificate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("audio_quality_capability_binding", source)
        self.assertNotIn(
            "from scripts.audio_production_contract_v2 import require_audio_production_contract_v2",
            source,
        )

    def test_registry_forbids_dynamic_quality_model_aliases(self) -> None:
        forbidden = {"openrouter/free", "openrouter/auto", "latest", "mistral-small-latest"}
        for capability, policy in router.capability_registry().items():
            for candidate in policy.candidates:
                self.assertNotIn(candidate.model, forbidden, capability)
                self.assertFalse(candidate.model.endswith("-latest"), capability)

    def test_router_source_has_no_provider_wire_transport(self) -> None:
        source = (_REPO_ROOT / "scripts/quality_capability_router.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("requests", imports)
        self.assertNotIn("httpx", imports)
        self.assertNotIn("google.genai", imports)
        self.assertNotIn("openai", imports)


if __name__ == "__main__":
    unittest.main()
