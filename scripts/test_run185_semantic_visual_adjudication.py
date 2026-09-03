from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import opening_feasibility_guard as opening_guard
from scripts import run183_visual_retrieval_closure as run183
from scripts import run183_visual_retrieval_scope_fix as scope_fix


RUN185_VISUAL = "A person standing between two people, drawing a line with a marker, looking thoughtful"
RUN185_NARRATION = (
    "الحدود الصحية تُبنى عندما نُعلنها بوضوح، حتى لو شعرنا بالذنب في البداية."
)
RUN185_ALTERNATE = "personal boundaries calm conversation"


class Run185SemanticVisualAdjudicationTests(unittest.TestCase):
    def assert_semantic_contract(self, value: str, *, contains: str) -> None:
        self.assertLessEqual(len(value), scope_fix.MAX_ENGINE_INTENDED_VISUAL_CHARS)
        self.assertIn("SEMANTIC POLICY:", value)
        self.assertIn("not literal shot checklist", value)
        self.assertIn("semantic-equivalent/contextual coverage", value)
        self.assertIn("reject generic unrelated B-roll", value)
        self.assertIn(contains, value)

    def test_run185_alternate_is_owned_by_current_semantic_family(self) -> None:
        trusted = scope_fix._trusted_semantic_intents(RUN185_VISUAL, RUN185_NARRATION)
        self.assertIn(RUN185_ALTERNATE, trusted)
        self.assertNotIn("marker closeup", trusted)

    def test_semantic_judgment_contract_is_bounded_and_explicitly_anti_literal(self) -> None:
        value = scope_fix._semantic_judgment_intent(RUN185_VISUAL)
        self.assert_semantic_contract(value, contains="A person standing between two people")
        self.assertNotIn("marker closeup", value)
        # Re-wrapping must be idempotent; nested wrappers cannot consume the 300-char
        # Engine field with duplicate policy text.
        self.assertEqual(scope_fix._semantic_judgment_intent(value), value)

    def test_semantic_alternate_reaches_vision_only_inside_runtime_scope(self) -> None:
        seen: list[str] = []

        def audit_fn(*args, **kwargs):
            seen.append(str(kwargs.get("intended_visual") or ""))
            return {"status": "block"}

        semantic_builder = scope_fix._semantic_recovery_stable_intent(
            opening_guard._stable_intent_audit
        )
        guarded_audit = semantic_builder(audit_fn, RUN185_VISUAL)
        trusted = scope_fix._trusted_semantic_intents(RUN185_VISUAL, RUN185_NARRATION)
        token = scope_fix._TRUSTED_SEMANTIC_INTENTS.set(trusted)
        try:
            with mock.patch.object(scope_fix, "_runtime_active", return_value=True):
                guarded_audit(intended_visual=RUN185_ALTERNATE)
            with mock.patch.object(scope_fix, "_runtime_active", return_value=False):
                guarded_audit(intended_visual=RUN185_ALTERNATE)
        finally:
            scope_fix._TRUSTED_SEMANTIC_INTENTS.reset(token)

        self.assertEqual(len(seen), 2)
        self.assert_semantic_contract(seen[0], contains=RUN185_ALTERNATE)
        self.assert_semantic_contract(seen[1], contains="A person standing between two people")
        self.assertNotEqual(seen[0], seen[1])

    def test_untrusted_search_syntax_still_uses_original_visual_truth(self) -> None:
        seen: list[str] = []

        def audit_fn(*args, **kwargs):
            seen.append(str(kwargs.get("intended_visual") or ""))
            return {"status": "block"}

        semantic_builder = scope_fix._semantic_recovery_stable_intent(
            opening_guard._stable_intent_audit
        )
        guarded_audit = semantic_builder(audit_fn, RUN185_VISUAL)
        token = scope_fix._TRUSTED_SEMANTIC_INTENTS.set(
            scope_fix._trusted_semantic_intents(RUN185_VISUAL, RUN185_NARRATION)
        )
        try:
            with mock.patch.object(scope_fix, "_runtime_active", return_value=True):
                guarded_audit(intended_visual="marker closeup")
                guarded_audit(intended_visual="between two drawing line marker")
        finally:
            scope_fix._TRUSTED_SEMANTIC_INTENTS.reset(token)

        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assert_semantic_contract(seen[0], contains="A person standing between two people")
        self.assertNotIn("marker closeup", seen[0])

    def test_selector_scope_binds_family_for_nested_vision_call_and_resets_afterward(self) -> None:
        observed: list[frozenset[str] | None] = []

        def selector(*args, **kwargs):
            observed.append(scope_fix._TRUSTED_SEMANTIC_INTENTS.get())
            return SimpleNamespace(reviewed=[])

        wrapped = scope_fix._selector_review_scope(selector)
        wrapped(
            intended_visual=RUN185_VISUAL,
            narration_context=RUN185_NARRATION,
        )

        self.assertEqual(len(observed), 1)
        self.assertIsNotNone(observed[0])
        self.assertIn(RUN185_ALTERNATE, observed[0])
        self.assertIsNone(scope_fix._TRUSTED_SEMANTIC_INTENTS.get())
        self.assertIsNone(scope_fix._REVIEWED_CURRENT_SELECTOR.get())

    def test_transient_provider_failure_semantics_are_preserved_for_semantic_recovery(self) -> None:
        class TimeoutErrorForTest(RuntimeError):
            pass

        def audit_fn(*args, **kwargs):
            raise TimeoutErrorForTest("HTTP 503 service_unavailable")

        semantic_builder = scope_fix._semantic_recovery_stable_intent(
            opening_guard._stable_intent_audit
        )
        guarded_audit = semantic_builder(audit_fn, RUN185_VISUAL)
        token = scope_fix._TRUSTED_SEMANTIC_INTENTS.set(
            scope_fix._trusted_semantic_intents(RUN185_VISUAL, RUN185_NARRATION)
        )
        try:
            with mock.patch.object(scope_fix, "_runtime_active", return_value=True):
                result = guarded_audit(intended_visual=RUN185_ALTERNATE)
        finally:
            scope_fix._TRUSTED_SEMANTIC_INTENTS.reset(token)

        self.assertEqual(result["status"], "block")
        self.assertEqual(result["review_origin"], "runner_vision_provider_call_failure")
        self.assertFalse(result["vision_review_performed"])
        self.assertFalse(result["semantic_verdict"])
        self.assertEqual(result["verdict_authority"], "technical_unavailable")

    def test_semantic_policy_versions_durable_vision_contract(self) -> None:
        base = lambda: "historical-vision-contract"
        wrapped = scope_fix._run185_contract_fingerprint(base)
        first = wrapped()
        second = wrapped()
        self.assertEqual(first, second)
        self.assertNotEqual(first, base())
        self.assertEqual(len(first), 64)
        self.assertTrue(getattr(wrapped, "_isco_run185_semantic_judgment_contract", False))
        self.assertEqual(
            scope_fix._run185_contract_fingerprint(wrapped),
            wrapped,
        )


if __name__ == "__main__":
    unittest.main()
