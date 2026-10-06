import unittest

from clean_v2 import providers


def _ok(name, log):
    def call(prompt, max_tokens, *a, **k):
        log.append(name)
        return {"ok": True}
    return call


def _adapters(log):
    names = ["gemini", "gemini_flash_lite", "groq", "openrouter", "mistral"]
    return tuple(providers.ProviderAdapter(n, _ok(n, log)) for n in names)


class StageProviderOrderTests(unittest.TestCase):
    def _router(self, log):
        r = providers.ProviderRouter(_adapters(log))
        r.stage_provider_order = providers.STAGE_PROVIDER_ORDER
        return r

    def _first(self, stage):
        log = []
        self._router(log).route(stage=stage, prompt="p", max_tokens=10, validator=lambda v: v)
        return log

    def test_first_provider_per_stage(self):
        self.assertEqual(self._first("planning"), ["mistral"])
        self.assertEqual(self._first("script"), ["gemini_flash_lite"])
        self.assertEqual(self._first("script_patch"), ["mistral"])

    def test_gemini_37_and_groq_never_used_in_production_stages(self):
        for stage, order in providers.STAGE_PROVIDER_ORDER.items():
            self.assertNotIn("gemini", order, stage)
            self.assertNotIn("groq", order, stage)

    def test_failover_follows_stage_order_then_stops(self):
        calls = []

        def boom(name):
            def call(prompt, max_tokens, *a, **k):
                calls.append(name)
                raise providers.ProviderWireFailure("http_500", http_status=500)
            return call

        names = ["gemini", "gemini_flash_lite", "groq", "openrouter", "mistral"]
        r = providers.ProviderRouter(tuple(providers.ProviderAdapter(n, boom(n)) for n in names))
        r.stage_provider_order = providers.STAGE_PROVIDER_ORDER
        with self.assertRaises(Exception):
            r.route(stage="script", prompt="p", max_tokens=10, validator=lambda v: v)
        self.assertEqual([c for c in calls if c in names], calls)
        self.assertNotIn("gemini", calls)
        self.assertNotIn("groq", calls)
        self.assertEqual(calls[0], "gemini_flash_lite")

    def test_unlisted_stage_keeps_default_order(self):
        log = []
        self._router(log).route(stage="narrative_identity", prompt="p", max_tokens=10, validator=lambda v: v)
        self.assertEqual(log, ["gemini"])

    def test_default_router_uses_stage_order_and_custom_does_not(self):
        self.assertIs(providers.ProviderRouter().stage_provider_order, providers.STAGE_PROVIDER_ORDER)
        custom = providers.ProviderRouter(_adapters([]))
        self.assertIsNone(custom.stage_provider_order)


if __name__ == "__main__":
    unittest.main()
