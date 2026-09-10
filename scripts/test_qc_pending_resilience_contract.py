from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class Run232ResilienceContractTests(unittest.TestCase):
    """Functional regression derived from Run #232 without extending the audited incident ledger."""

    def test_budget_is_persisted_before_qc_pending_capture(self) -> None:
        source = (ROOT / "run_v3_voice.py").read_text(encoding="utf-8")
        marker = "except Exception as exc:\n        # Preserve the enforcing failure as the workflow result"
        start = source.index(marker)
        end = source.index("\n\n    run_post_gold_observers(out)", start)
        block = source[start:end]
        budget = block.index('ledger.write(out / "ai-budget.json")')
        audio = block.index("capture_audio_qc_pending_checkpoint(out, exc)")
        gold = block.index("capture_qc_pending_checkpoint(out, exc)")
        self.assertLess(budget, audio)
        self.assertLess(budget, gold)
        self.assertEqual(block.count('ledger.write(out / "ai-budget.json")'), 1)

    def test_cloudflare_preflight_reuses_runtime_zero_cost_probes(self) -> None:
        source = (ROOT / "cloudflare_gold_preflight.py").read_text(encoding="utf-8")
        self.assertIn("_credentials", source)
        self.assertIn("_prove_workers_free", source)
        self.assertIn("_prove_model_access", source)
        self.assertNotIn("requests.get", source)
        self.assertNotIn("requests.post", source)

    def test_optional_cloudflare_preflight_disables_only_provider_unavailability(self) -> None:
        source = (ROOT / "cloudflare_gold_preflight.py").read_text(encoding="utf-8")
        start = source.index("def preflight_optional_gold_cloudflare_from_runner_temp()")
        block = source[start:]
        self.assertIn("except CloudflareGoldVisionUnavailable as exc:", block)
        self.assertIn("_disable_optional_route_for_following_steps()", block)
        self.assertIn("production continues without Cloudflare Gold fallback", block)
        self.assertNotIn("except Exception", block)

    def test_provider_preflight_invokes_optional_cloudflare_before_returning(self) -> None:
        source = (ROOT / "provider_preflight.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        calls = [
            node.func.id
            for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertIn("_original_main", calls)
        self.assertIn("preflight_optional_gold_cloudflare_from_runner_temp", calls)
        self.assertNotIn("preflight_gold_cloudflare_from_runner_temp", calls)

    def test_telegram_progress_attests_before_persisting_message_id(self) -> None:
        source = (ROOT / "telegram_progress.py").read_text(encoding="utf-8")
        start = source.index("def start_progress()")
        end = source.index("\n\ndef update_stage", start)
        block = source[start:end]
        response_attestation = block.index("attest_message_response(resp, expected_chat_id=chat_id)")
        persist = block.index('_state["message_id"] = message_id')
        disk_write = block.index('f.write(str(message_id))')
        self.assertLess(response_attestation, persist)
        self.assertLess(response_attestation, disk_write)

    def test_telegram_edits_are_bound_to_same_message(self) -> None:
        source = (ROOT / "telegram_progress.py").read_text(encoding="utf-8")
        start = source.index("def update_stage(stage: str)")
        end = source.index("\n\ndef advance_stage", start)
        block = source[start:end]
        self.assertIn("expected_chat_id=_state[\"chat_id\"]", block)
        self.assertIn("expected_message_id=_state[\"message_id\"]", block)
        self.assertIn('_state["message_id"] = None', block)


if __name__ == "__main__":
    unittest.main()
