from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import dynamic_planning_capacity as dynamic
from scripts import immutable_planning_snapshot as snapshot
from scripts import planning_checkpoint_state as checkpoint
from scripts import provider_capacity_hardening as capacity


ROOT = Path(__file__).resolve().parents[1]
MODEL_20B = "openai/gpt-oss-20b"
MODEL_120B = "openai/gpt-oss-120b"


class _Response:
    def __init__(self, status_code: int, payload: dict, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class Run126RootCauseClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_build_runtime_binding = checkpoint.build_runtime_binding
        self.original_contract_files = checkpoint.PLANNING_CONTRACT_FILES
        self.original_snapshot_installed = snapshot._INSTALLED
        capacity.reset_groq_capacity_state_for_tests()

    def tearDown(self) -> None:
        checkpoint.build_runtime_binding = self.original_build_runtime_binding
        checkpoint.PLANNING_CONTRACT_FILES = self.original_contract_files
        snapshot._INSTALLED = self.original_snapshot_installed
        capacity.reset_groq_capacity_state_for_tests()

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def _engine_repo(self, root: Path, brief_bytes: bytes) -> tuple[Path, str]:
        engine = root / "engine"
        engine.mkdir()
        self._git(engine, "init")
        self._git(engine, "config", "user.name", "test")
        self._git(engine, "config", "user.email", "test@example.com")
        brief = engine / "production" / "approved_brief.json"
        brief.parent.mkdir(parents=True)
        brief.write_bytes(brief_bytes)
        (engine / "README.md").write_text("engine\n", encoding="utf-8")
        self._git(engine, "add", ".")
        self._git(engine, "commit", "-m", "engine")
        return engine, self._git(engine, "rev-parse", "HEAD")

    def test_exact_run126_chain_zero_sleep_no_viable_and_partial_checkpoint_survives_mutable_brief(self) -> None:
        """20B TPD exhausted -> 120B actual TPM=800 -> zero sleep -> no viable -> checkpoint survives brief mutation."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            groq_key = root / "groq-key"
            groq_key.write_text("test-key", encoding="utf-8")

            provider_preflight = runner_temp / "provider-preflight.json"
            provider_preflight.write_text(
                json.dumps(
                    {
                        "checks": [
                            {"provider": "gemini", "status": "block"},
                            {"provider": "groq", "status": "pass"},
                            {"provider": "openrouter", "status": "block"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            brief_bytes = b'{"approved_by_user":true,"approved_topic":"run126","format":"film"}\n'
            expected_brief_sha = hashlib.sha256(brief_bytes).hexdigest()
            engine, engine_sha = self._engine_repo(root, brief_bytes)
            snapshot_path = runner_temp / "isco-state" / "approved-brief.snapshot.json"

            env = {
                "RUNNER_TEMP": str(runner_temp),
                "GROQ_API_KEY_FILE": str(groq_key),
                "ISCO_PROVIDER_PREFLIGHT_PATH": str(provider_preflight),
                "ISCO_APPROVED_BRIEF_SNAPSHOT_PATH": str(snapshot_path),
                "ISCO_APPROVED_BRIEF_SHA256": expected_brief_sha,
                "ISCO_ENGINE_SHA": engine_sha,
            }
            with patch.dict(os.environ, env, clear=False):
                snapshot.snapshot_approved_brief(
                    engine / "production" / "approved_brief.json",
                    snapshot_path,
                    expected_sha256=expected_brief_sha,
                )
                snapshot.install_runtime_snapshot_binding(force=True)
                binding_before = checkpoint.build_runtime_binding(ROOT, engine)

                # Exact first root: 20B's free daily quota is exhausted. The body has a
                # large bare Limit value, but because its context is TPD it must NEVER
                # be recorded as actual_tpm_limit. The state remains model-scoped so
                # 120B is still eligible for one real contact.
                tpd_message = (
                    "Rate limit reached on tokens per day (TPD): "
                    "Limit 200000, Used 200000, Requested 3000"
                )
                self.assertIsNone(capacity._limit_from_error_text(tpd_message))
                tpd = _Response(
                    429,
                    {
                        "error": {
                            "type": "rate_limit_exceeded",
                            "message": tpd_message,
                        }
                    },
                )
                state_20b = capacity.observe_groq_response(tpd, MODEL_20B, required_tokens=3000)
                self.assertTrue(capacity.groq_model_blocked(MODEL_20B))
                self.assertIsNone(state_20b["actual_tpm_limit"])
                self.assertIsNone(capacity.groq_effective_tpm_limit(MODEL_20B))
                self.assertEqual(
                    capacity.groq_admission_decision(MODEL_20B, 3000)["action"],
                    "unavailable",
                )
                self.assertIn(
                    capacity.groq_admission_decision(MODEL_120B, 3000)["action"],
                    {"admit", "unknown"},
                )

                # Exact second root: 120B accepts contact but reveals an actual TPM of
                # only 800. required > actual_limit is mathematically impossible and
                # therefore must never be treated as a waitable remaining-window case.
                tpm_message = (
                    "Rate limit reached on tokens per minute (TPM): "
                    "Limit 800, Used 740, Requested 2600"
                )
                self.assertEqual(capacity._limit_from_error_text(tpm_message), 800)
                tpm_800 = _Response(
                    429,
                    {
                        "error": {
                            "type": "rate_limit_exceeded",
                            "message": tpm_message,
                        }
                    },
                )
                with patch.object(dynamic.router.requests, "post", return_value=tpm_800) as post, patch.object(
                    capacity.time, "sleep"
                ) as capacity_sleep:
                    with self.assertRaisesRegex(RuntimeError, "GROQ_ACTUAL_TPM_BELOW_REQUEST"):
                        dynamic._dynamic_groq_model_call("RUN126_WRITER_SHARD", MODEL_120B)

                self.assertEqual(post.call_count, 1)
                capacity_sleep.assert_not_called()
                learned = capacity.groq_admission_decision(MODEL_120B, 2600)
                self.assertEqual(learned["action"], "impossible")
                self.assertEqual(learned["reason"], "actual_limit_below_required")
                self.assertEqual(learned["actual_limit"], 800)

                state_path = runner_temp / "isco-state" / "groq-model-capacity-v1.json"
                persisted_capacity = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    persisted_capacity["models"][MODEL_20B]["blocked_reason"],
                    "daily_token_quota_exhausted",
                )
                self.assertIsNone(
                    persisted_capacity["models"][MODEL_20B]["actual_tpm_limit"]
                )
                self.assertEqual(
                    persisted_capacity["models"][MODEL_120B]["actual_tpm_limit"],
                    800,
                )

                # Gemini/OpenRouter were already blocked by preflight, 20B is TPD-dead,
                # and 120B cannot fit the request. The gate must fail immediately rather
                # than spending minutes in planning retries/waits.
                with self.assertRaisesRegex(RuntimeError, "NO_VIABLE_PLANNING_CAPACITY"):
                    dynamic.require_viable_planning_capacity(
                        2600,
                        phase="exact_runtime_writer",
                        preflight_path=provider_preflight,
                    )

                # Build a partial checkpoint while the snapshot still matches. Then
                # mutate the Engine working-tree brief exactly like Run126 did. Binding
                # and encryption must stay attached to the immutable snapshot bytes.
                plain = root / "partial-checkpoint.json"
                identity = root / "identity.json"
                encrypted = root / "partial-checkpoint.json.enc"
                response_key = hashlib.sha256(b"S1").hexdigest()
                plain.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "responses": {
                                response_key: {
                                    "sections": [
                                        {"id": "S1", "narration": "done", "key_point": "done"}
                                    ]
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                checkpoint._write_identity(
                    identity,
                    checkpoint.RestoreStatus(True, False, "empty", "test bootstrap"),
                    binding_before,
                )

                mutable_brief = engine / "production" / "approved_brief.json"
                mutable_brief.write_text(
                    '{"approved_by_user":true,"approved_topic":"MUTATED_AFTER_START","format":"film"}\n',
                    encoding="utf-8",
                )
                self.assertNotEqual(hashlib.sha256(mutable_brief.read_bytes()).hexdigest(), expected_brief_sha)
                self.assertEqual(hashlib.sha256(snapshot_path.read_bytes()).hexdigest(), expected_brief_sha)
                self.assertEqual(snapshot_path.stat().st_mode & 0o222, 0)

                binding_after = checkpoint.build_runtime_binding(ROOT, engine)
                self.assertEqual(binding_after, binding_before)
                self.assertTrue(
                    checkpoint._encrypt(
                        plain,
                        identity,
                        encrypted,
                        "run126-test-key",
                        binding_after,
                        run_number="126",
                        status="in_progress",
                    )
                )
                wrapper, metadata = checkpoint._decode(encrypted.read_bytes(), "run126-test-key")
                self.assertEqual(wrapper["binding"], binding_before.as_dict())
                self.assertIn(response_key, wrapper["checkpoint"]["responses"])
                self.assertEqual(metadata.sequence, 126)

    def test_remaining_below_required_is_waitable_only_when_actual_limit_can_fit(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"RUNNER_TEMP": td}, clear=False
        ):
            capacity.reset_groq_capacity_state_for_tests()
            response = _Response(
                200,
                {"choices": []},
                headers={
                    "x-ratelimit-limit-tokens": "8000",
                    "x-ratelimit-remaining-tokens": "500",
                    "x-ratelimit-reset-tokens": "2s",
                },
            )
            capacity.observe_groq_response(response, MODEL_120B, required_tokens=2600)
            decision = capacity.groq_admission_decision(MODEL_120B, 2600)
            self.assertEqual(decision["action"], "wait")
            self.assertEqual(decision["reason"], "remaining_below_required")

            with patch.object(capacity.time, "sleep") as sleep:
                delayed = capacity._proactive_groq_pacing(
                    {"estimated_request_tokens": 2600},
                    model_name=MODEL_120B,
                )
            self.assertGreater(delayed, 0.0)
            sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
