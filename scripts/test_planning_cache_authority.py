from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import planning_cache_authority as authority
from scripts import task_level_planner_router as router

import isco_video_agent.resilient_planner as staged


def _full_script_prompt(ids: list[str]) -> str:
    specs = json.dumps(
        [{"id": section_id, "purpose": f"purpose-{section_id}", "transition_hint": ""} for section_id in ids],
        ensure_ascii=False,
    )
    return (
        "Section specs (id, purpose, transition_hint) — write exactly one narration per entry, in this exact order:\n"
        + specs
        + "\nReturn ONLY JSON: {\"sections\": []} with EXACTLY "
        + str(len(ids))
        + " entries, in this exact order, using these exact ids."
    )


def _script(ids: list[str]) -> dict:
    return {
        "sections": [
            {"id": section_id, "narration": f"نص {section_id}", "key_point": f"key-{section_id}"}
            for section_id in ids
        ]
    }


class PlanningCacheAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        self._sleep_patch = patch.object(router.time, "sleep")
        self._sleep_patch.start()

        # Keep tests isolated even when another test in the same interpreter installed
        # the write-side authority wrapper first.
        original = getattr(router._normalize_outline, "_isco_planning_cache_authority_original", None)
        if original is not None:
            router._normalize_outline = original

    def tearDown(self) -> None:
        self._sleep_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _install(self) -> None:
        authority.install_cache_authority_pre_router()
        router.install_router()
        authority.install_cache_authority_post_router()

    def test_valid_json_with_wrong_exact_ids_is_not_cached_and_falls_back(self) -> None:
        """Run137 regression: shape/count-valid JSON with wrong ids must never gain cache authority."""
        prompt = _full_script_prompt(["s1", "s2", "s3"])
        poisoned = _script(["s1", "WRONG", "s3"])
        valid = _script(["s1", "s2", "s3"])
        gemini_calls = 0
        groq_calls = 0

        def fake_gemini_json_text(api_key, provider_prompt, model):
            nonlocal gemini_calls
            del api_key, provider_prompt, model
            gemini_calls += 1
            return poisoned

        def fake_groq_call(provider_prompt):
            nonlocal groq_calls
            del provider_prompt
            groq_calls += 1
            return valid

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", side_effect=fake_groq_call):
            self._install()
            result = staged.json_text("unused-api-key", prompt, model="gemini-2.5-flash")

        self.assertEqual(result, valid)
        self.assertEqual(gemini_calls, 1)
        self.assertEqual(groq_calls, 1)
        checkpoint = json.loads(router.CACHE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(list(checkpoint["responses"].values()), [valid])

    def test_shape_invalid_response_is_not_cached(self) -> None:
        prompt = _full_script_prompt(["s1", "s2", "s3"])
        malformed_shape = _script(["s1", "s2", "s3"])
        del malformed_shape["sections"][1]["key_point"]
        valid = _script(["s1", "s2", "s3"])

        with patch.object(router, "gemini_json_text", return_value=malformed_shape), \
                patch.object(router, "_groq_call", return_value=valid):
            self._install()
            result = staged.json_text("unused-api-key", prompt, model="gemini-2.5-flash")

        self.assertEqual(result, valid)
        checkpoint = json.loads(router.CACHE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(list(checkpoint["responses"].values()), [valid])

    def test_poisoned_restored_response_is_evicted_before_provider_routing(self) -> None:
        prompt = _full_script_prompt(["s1", "s2", "s3"])
        poisoned = _script(["s1", "WRONG", "s3"])
        valid = _script(["s1", "s2", "s3"])
        model = "gemini-2.5-flash"
        cache_key = authority._cache_key(prompt, model)
        router.CACHE_PATH.write_text(
            json.dumps({"version": 1, "responses": {cache_key: poisoned}}, ensure_ascii=False),
            encoding="utf-8",
        )
        provider_calls = 0

        def fake_gemini_json_text(api_key, provider_prompt, provider_model):
            nonlocal provider_calls
            del api_key, provider_prompt, provider_model
            provider_calls += 1
            return valid

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text):
            self._install()
            result = staged.json_text("unused-api-key", prompt, model=model)

        self.assertEqual(result, valid)
        self.assertEqual(provider_calls, 1)
        checkpoint = json.loads(router.CACHE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["responses"].get(cache_key), valid)


if __name__ == "__main__":
    unittest.main()
