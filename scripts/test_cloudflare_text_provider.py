import os
import unittest
from unittest import mock

from clean_v2 import providers
from clean_v2.providers import NoWireFailure


class CloudflareTextProviderTests(unittest.TestCase):
    def test_missing_credentials_is_no_wire(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "", "CLOUDFLARE_ACCOUNT_ID": ""}, clear=False):
            with self.assertRaises(NoWireFailure):
                providers._cloudflare_call("x", 100)

    def test_unapproved_model_is_refused(self):
        env = {"CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_TEXT_MODEL": "@cf/some/paid-model"}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(NoWireFailure):
                providers._cloudflare_call("x", 100)

    def test_parses_openai_compatible_response(self):
        env = {"CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_ACCOUNT_ID": "a" * 32}
        seen = {}

        def fake_post(url, **kw):
            seen["url"] = url
            seen["payload"] = kw["payload"]
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(providers, "_post_json", fake_post):
            out = providers._cloudflare_call("hi", 500)
        self.assertEqual(out, {"ok": True})
        self.assertIn("/ai/v1/chat/completions", seen["url"])
        self.assertEqual(seen["payload"]["model"], "@cf/openai/gpt-oss-120b")

    def test_per_run_cap_reserves_quota_for_vision(self):
        env = {"CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_ACCOUNT_ID": "a" * 32}
        ok = lambda url, **kw: {"choices": [{"message": {"content": "{}"}}]}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(providers, "_post_json", ok), mock.patch.object(providers, "_cloudflare_text_calls", 0):
            for _ in range(providers.CLOUDFLARE_TEXT_MAX_CALLS_PER_RUN):
                providers._cloudflare_call("x", 10)
            with self.assertRaises(NoWireFailure):
                providers._cloudflare_call("x", 10)

    def test_in_stage_orders_before_openrouter(self):
        for stage in ("planning", "script", "script_patch"):
            order = providers.STAGE_PROVIDER_ORDER[stage]
            self.assertLess(order.index("cloudflare"), order.index("openrouter"))
        self.assertIn("cloudflare", [a.name for a in providers.default_adapters()])


if __name__ == "__main__":
    unittest.main()
