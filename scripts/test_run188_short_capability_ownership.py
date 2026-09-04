from __future__ import annotations

import inspect
import os
import unittest

from scripts import canonical_v4_short_child, run_control_production
from scripts import short_cinematic_director, short_voice_v2
from scripts.short_finishing_capabilities import (
    ShortFinishingCapabilities,
    ShortFinishingCapabilityError,
    bind_short_finishing_capabilities,
)


class Run188ShortCapabilityOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_voice_secret = short_voice_v2.secret
        cls._original_cinematic_secret = short_cinematic_director.secret

    @classmethod
    def tearDownClass(cls) -> None:
        # The production bridge is process-stable by design, but this unit module must
        # not leak that runtime mutation into unrelated tests sharing the same worker.
        short_voice_v2.secret = cls._original_voice_secret
        short_cinematic_director.secret = cls._original_cinematic_secret

    def test_capabilities_never_expose_secret_material_in_repr(self) -> None:
        capabilities = ShortFinishingCapabilities(
            gemini="gemini-sensitive-value",
            pexels="pexels-sensitive-value",
            pixabay="pixabay-sensitive-value",
        )
        rendered = repr(capabilities)
        self.assertNotIn("gemini-sensitive-value", rendered)
        self.assertNotIn("pexels-sensitive-value", rendered)
        self.assertNotIn("pixabay-sensitive-value", rendered)

    def test_gold_kwargs_are_the_only_capability_source(self) -> None:
        capabilities = ShortFinishingCapabilities.from_gold_kwargs(
            {
                "gemini": "owned-gemini",
                "pexels": "owned-pexels",
                "pixabay": "owned-pixabay",
            }
        )
        self.assertEqual(capabilities.gemini, "owned-gemini")
        self.assertEqual(capabilities.pexels, "owned-pexels")
        self.assertEqual(capabilities.pixabay, "owned-pixabay")
        with self.assertRaisesRegex(
            ShortFinishingCapabilityError,
            "SHORT_FINISHING_CAPABILITIES_MISSING",
        ):
            ShortFinishingCapabilities.from_gold_kwargs({"gemini": "owned-gemini"})

    def test_finishing_uses_scoped_memory_and_never_reconsumes_source_environment(self) -> None:
        names = ("GEMINI_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY")
        previous = {name: os.environ.get(name) for name in names}
        poison = {
            "GEMINI_API_KEY": "ENV-MUST-NOT-BE-READ-G",
            "PEXELS_API_KEY": "ENV-MUST-NOT-BE-READ-P",
            "PIXABAY_API_KEY": "ENV-MUST-NOT-BE-READ-X",
        }
        os.environ.update(poison)
        try:
            capabilities = ShortFinishingCapabilities(
                gemini="owned-g",
                pexels="owned-p",
                pixabay="owned-x",
            )
            with bind_short_finishing_capabilities(capabilities):
                self.assertEqual(short_voice_v2.secret("GEMINI_API_KEY"), "owned-g")
                self.assertEqual(short_cinematic_director.secret("GEMINI_API_KEY"), "owned-g")
                self.assertEqual(short_cinematic_director.secret("PEXELS_API_KEY"), "owned-p")
                self.assertEqual(short_cinematic_director.secret("PIXABAY_API_KEY"), "owned-x")
                self.assertEqual({name: os.environ.get(name) for name in names}, poison)

                with self.assertRaisesRegex(
                    ShortFinishingCapabilityError,
                    "SHORT_VOICE_CAPABILITY_NOT_ALLOWED",
                ):
                    short_voice_v2.secret("PEXELS_API_KEY")

            # Resolver remains fail-closed after the lease ends; it never falls back to
            # the still-present poison environment value.
            with self.assertRaisesRegex(
                ShortFinishingCapabilityError,
                "SHORT_FINISHING_CAPABILITY_CONTEXT_MISSING",
            ):
                short_voice_v2.secret("GEMINI_API_KEY")
            self.assertEqual({name: os.environ.get(name) for name in names}, poison)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_nested_scopes_restore_previous_request_without_cross_run_leakage(self) -> None:
        outer = ShortFinishingCapabilities(gemini="outer-g", pexels="outer-p")
        inner = ShortFinishingCapabilities(gemini="inner-g", pexels="inner-p")
        with bind_short_finishing_capabilities(outer):
            self.assertEqual(short_voice_v2.secret("GEMINI_API_KEY"), "outer-g")
            with bind_short_finishing_capabilities(inner):
                self.assertEqual(short_voice_v2.secret("GEMINI_API_KEY"), "inner-g")
            self.assertEqual(short_voice_v2.secret("GEMINI_API_KEY"), "outer-g")

    def test_standalone_and_sibling_callers_bind_exact_gold_kwargs_before_finishing(self) -> None:
        standalone = inspect.getsource(run_control_production.execute_control_request)
        sibling = inspect.getsource(canonical_v4_short_child.execute)
        for source in (standalone, sibling):
            capabilities_at = source.index("ShortFinishingCapabilities.from_gold_kwargs(kwargs)")
            bind_at = source.index("with bind_short_finishing_capabilities(capabilities):")
            finish_at = source.index("prepare_authoritative_short_for_gold(")
            gold_at = source.index("result = original_gold(**kwargs)")
            self.assertLess(capabilities_at, bind_at)
            self.assertLess(bind_at, finish_at)
            self.assertLess(finish_at, gold_at)

    def test_capability_adapter_contains_no_source_secret_reader_or_env_reinjection(self) -> None:
        from scripts import short_finishing_capabilities as adapter

        source = inspect.getsource(adapter)
        self.assertNotIn("from isco_video_agent.config import secret", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("Path(", source)
        self.assertIn("ContextVar", source)
        self.assertIn("field(repr=False)", source)


if __name__ == "__main__":
    unittest.main()
