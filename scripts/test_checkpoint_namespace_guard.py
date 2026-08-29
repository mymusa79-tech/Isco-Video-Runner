from __future__ import annotations

import hashlib
import os
import unittest

import scripts.task_level_planner_router as router
from scripts import planning_checkpoint_state_core as durable_state
from scripts.checkpoint_namespace_guard import (
    CHECKPOINT_NAMESPACE_SCHEMA_VERSION,
    CHECKPOINT_SCHEMA_VERSION,
    checkpoint_namespace,
    install_checkpoint_namespace_guard,
)


class CheckpointNamespaceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_load = router._load_checkpoint
        self.original_save = router._save_checkpoint
        self.old_env = dict(os.environ)

    def tearDown(self) -> None:
        router._load_checkpoint = self.original_load
        router._save_checkpoint = self.original_save
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_namespace_changes_when_runner_or_engine_revision_changes(self) -> None:
        os.environ["GITHUB_SHA"] = "a" * 40
        os.environ["ISCO_ENGINE_SHA"] = "b" * 40
        first = checkpoint_namespace()
        os.environ["ISCO_ENGINE_SHA"] = "c" * 40
        self.assertNotEqual(first, checkpoint_namespace())
        os.environ["ISCO_ENGINE_SHA"] = "b" * 40
        os.environ["GITHUB_SHA"] = "d" * 40
        self.assertNotEqual(first, checkpoint_namespace())

    def test_namespace_recipe_version_is_independent_from_document_schema(self) -> None:
        self.assertEqual(CHECKPOINT_SCHEMA_VERSION, 1)
        self.assertEqual(CHECKPOINT_NAMESPACE_SCHEMA_VERSION, 2)

    def test_old_checkpoint_is_invalidated_instead_of_reused(self) -> None:
        os.environ["GITHUB_SHA"] = "a" * 40
        os.environ["ISCO_ENGINE_SHA"] = "b" * 40
        router._load_checkpoint = lambda: {
            "version": 1,
            "responses": {"old-key": {"stale": True}},
        }
        router._save_checkpoint = lambda data: None
        install_checkpoint_namespace_guard()
        data = router._load_checkpoint()
        self.assertEqual(data["version"], CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(data["namespace"], checkpoint_namespace())
        self.assertEqual(data["responses"], {})

    def test_same_runtime_namespace_preserves_checkpoint(self) -> None:
        os.environ["GITHUB_SHA"] = "a" * 40
        os.environ["ISCO_ENGINE_SHA"] = "b" * 40
        expected = checkpoint_namespace()
        router._load_checkpoint = lambda: {
            "version": CHECKPOINT_SCHEMA_VERSION,
            "namespace": expected,
            "responses": {"key": {"ok": True}},
        }
        router._save_checkpoint = lambda data: None
        install_checkpoint_namespace_guard()
        self.assertEqual(router._load_checkpoint()["responses"], {"key": {"ok": True}})

    def test_guarded_router_checkpoint_is_accepted_by_durable_persistence_contract(self) -> None:
        """Run132 regression: the live namespace layer and durable writer must compose."""
        os.environ["GITHUB_SHA"] = "a" * 40
        os.environ["ISCO_ENGINE_SHA"] = "b" * 40
        captured: dict = {}
        router._load_checkpoint = lambda: {"version": 1, "responses": {}}
        router._save_checkpoint = lambda data: captured.update(data)
        install_checkpoint_namespace_guard()

        cache_key = hashlib.sha256(b"run132-composed-checkpoint").hexdigest()
        router._save_checkpoint(
            {
                "version": 1,
                "responses": {
                    cache_key: {
                        "sections": [
                            {"id": "S1", "narration": "done", "key_point": "done"}
                        ]
                    }
                },
            }
        )

        normalized = durable_state._normalize_checkpoint(captured)
        self.assertEqual(normalized["version"], durable_state.EMPTY_CHECKPOINT["version"])
        self.assertEqual(normalized["responses"], captured["responses"])
        self.assertEqual(captured["namespace"], checkpoint_namespace())


if __name__ == "__main__":
    unittest.main()
