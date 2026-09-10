from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.visual_failure_memory_checkpoint import (
    MAX_ENTRIES,
    MEMORY_KEY,
    MEMORY_SCHEMA,
    prepare_failure_memory_checkpoint,
)


def _memory(asset_id: str = "42") -> dict:
    return {
        "schema": MEMORY_SCHEMA,
        "updated_at": "2026-09-10T00:00:00Z",
        "contextual_ttl_hours": 336,
        "asset_global_ttl_hours": 720,
        "max_entries": MAX_ENTRIES,
        "entries": [
            {
                "provider": "pexels",
                "asset_id": asset_id,
                "scope": "contextual",
                "reason_code": "final_cut_semantic_floor_below_target",
                "context_hash": "ctx-a",
                "first_seen_at": "2026-09-10T00:00:00Z",
                "last_seen_at": "2026-09-10T00:00:00Z",
                "expires_at": "2026-09-24T00:00:00Z",
                "fail_count": 1,
                "intended_terms": ["person", "desk", "starting"],
                "narration_terms": ["beginning", "work", "motivation"],
            }
        ],
    }


class VisualFailureMemoryCheckpointTests(unittest.TestCase):
    def _paths(self) -> tuple[tempfile.TemporaryDirectory, Path, Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        return tmp, root / "baseline.json", root / "runtime.json", root / "checkpoint.json"

    def test_only_failure_memory_crosses_from_failed_runtime(self) -> None:
        tmp, baseline_path, runtime_path, output_path = self._paths()
        self.addCleanup(tmp.cleanup)
        baseline = {
            "videos": [{"topic": "accepted-before-run"}],
            "other_state": {"keep": "baseline"},
        }
        runtime = {
            "videos": [
                {"topic": "accepted-before-run"},
                {"topic": "must-not-leak-from-failed-run"},
            ],
            "other_state": {"keep": "runtime-mutation-must-not-cross"},
            MEMORY_KEY: _memory(),
        }
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

        changed = prepare_failure_memory_checkpoint(
            baseline_path=baseline_path,
            runtime_path=runtime_path,
            output_path=output_path,
        )

        self.assertIs(changed, True)
        checkpoint = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["videos"], baseline["videos"])
        self.assertEqual(checkpoint["other_state"], baseline["other_state"])
        self.assertEqual(checkpoint[MEMORY_KEY], runtime[MEMORY_KEY])
        self.assertNotIn("must-not-leak-from-failed-run", json.dumps(checkpoint))
        self.assertNotIn("runtime-mutation-must-not-cross", json.dumps(checkpoint))

    def test_unchanged_or_absent_memory_creates_no_checkpoint(self) -> None:
        tmp, baseline_path, runtime_path, output_path = self._paths()
        self.addCleanup(tmp.cleanup)
        same = {"videos": [], MEMORY_KEY: _memory()}
        baseline_path.write_text(json.dumps(same), encoding="utf-8")
        runtime_path.write_text(json.dumps(same), encoding="utf-8")
        output_path.write_text("stale", encoding="utf-8")

        changed = prepare_failure_memory_checkpoint(
            baseline_path=baseline_path,
            runtime_path=runtime_path,
            output_path=output_path,
        )

        self.assertIs(changed, False)
        self.assertFalse(output_path.exists())

    def test_invalid_memory_schema_fails_closed(self) -> None:
        tmp, baseline_path, runtime_path, output_path = self._paths()
        self.addCleanup(tmp.cleanup)
        baseline_path.write_text(json.dumps({"videos": []}), encoding="utf-8")
        runtime_path.write_text(
            json.dumps({"videos": [], MEMORY_KEY: {"schema": "wrong", "entries": []}}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "schema mismatch"):
            prepare_failure_memory_checkpoint(
                baseline_path=baseline_path,
                runtime_path=runtime_path,
                output_path=output_path,
            )

    def test_oversized_entry_set_fails_closed(self) -> None:
        tmp, baseline_path, runtime_path, output_path = self._paths()
        self.addCleanup(tmp.cleanup)
        baseline_path.write_text(json.dumps({"videos": []}), encoding="utf-8")
        memory = _memory()
        memory["entries"] = [dict(memory["entries"][0], asset_id=str(index)) for index in range(MAX_ENTRIES + 1)]
        runtime_path.write_text(json.dumps({"videos": [], MEMORY_KEY: memory}), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "exceeds Engine entry cap"):
            prepare_failure_memory_checkpoint(
                baseline_path=baseline_path,
                runtime_path=runtime_path,
                output_path=output_path,
            )

    def test_contextual_entry_requires_context_hash(self) -> None:
        tmp, baseline_path, runtime_path, output_path = self._paths()
        self.addCleanup(tmp.cleanup)
        baseline_path.write_text(json.dumps({"videos": []}), encoding="utf-8")
        memory = _memory()
        memory["entries"][0]["context_hash"] = ""
        runtime_path.write_text(json.dumps({"videos": [], MEMORY_KEY: memory}), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "lacks context hash"):
            prepare_failure_memory_checkpoint(
                baseline_path=baseline_path,
                runtime_path=runtime_path,
                output_path=output_path,
            )


if __name__ == "__main__":
    unittest.main()
