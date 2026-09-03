from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from isco_video_agent.ai_budget import Capability, Priority, TaskSpec
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as closure
from scripts import vision_provider_reliability as legacy
from scripts import vision_stage_contract_v2 as v2


_PASS = {
    "status": "pass",
    "relevance": 0.93,
    "visual_quality": 0.90,
    "identifiable_person": False,
    "sensitive_trait_implication_risk": False,
    "prominent_logo_or_brand": False,
    "cultural_conflict": False,
    "cultural_islamic_suitability_risk": False,
    "advertiser_conflict": False,
    "obvious_synthetic_or_visual_artifact": False,
    "reason": "safe and relevant",
}


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id="VISUAL_AUDIT_RUN181",
        kind="VISUAL_AUDIT",
        priority=Priority.P0,
        capability=Capability.VISION,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=False,
    )


def _preview(root: str) -> Path:
    path = Path(root) / "preview.mp4"
    path.write_bytes(b"preview")
    return path


class ProviderHealthRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        closure._GROQ_MODEL_CERTIFIED.set(None)

    def test_health_is_scoped_by_model_and_quota_domain(self) -> None:
        health.publish_provider_unavailable(
            "gemini",
            model="gemini-3.7-flash",
            quota_domain="generate_content",
            reason="429 quota",
            source="planning",
        )
        self.assertIsNotNone(
            health.provider_unavailable(
                "gemini", model="gemini-3.7-flash", quota_domain="generate_content"
            )
        )
        self.assertIsNone(
            health.provider_unavailable(
                "gemini", model="gemini-3.1-flash-tts-preview", quota_domain="tts"
            )
        )

    def test_preflight_provider_wide_block_is_imported_without_false_pass_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "provider-preflight.json"
            path.write_text(
                json.dumps(
                    {
                        "checks": [
                            {"provider": "gemini", "status": "pass", "detail": "dynamic"},
                            {
                                "provider": "openrouter",
                                "status": "block",
                                "detail": "key spend capacity exhausted",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            health.load_preflight_provider_health(path)
        self.assertIsNone(
            health.provider_unavailable(
                "gemini", model="gemini-3.7-flash", quota_domain="generate_content"
            )
        )
        evidence = health.provider_unavailable(
            "openrouter", model="openrouter/free", quota_domain="vision"
        )
        self.assertIsNotNone(evidence)
        self.assertIn("spend capacity exhausted", evidence.reason)


class GroqVisionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        closure._GROQ_MODEL_CERTIFIED.set(None)

    def test_groq_vision_uses_three_sampled_images_and_exact_strict_schema(self) -> None:
        class CatalogResponse:
            ok = True
            status_code = 200

            def json(self):
                return {"data": [{"id": closure.GROQ_VISION_MODEL}]}

        class ChatResponse:
            ok = True
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": json.dumps(_PASS)}}]}

        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"GROQ_API_KEY": "test-key"}, clear=False
        ), mock.patch.object(
            closure.requests, "get", return_value=CatalogResponse()
        ), mock.patch.object(
            closure.requests, "post", return_value=ChatResponse()
        ) as post, mock.patch.object(
            v2.legacy, "_sample_preview_frames", return_value=[b"a", b"b", b"c"]
        ):
            result = closure._groq_visual_call(
                _preview(root), narration_context="ctx", intended_visual="intent"
            )
        self.assertEqual(result["status"], "pass")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], closure.GROQ_VISION_MODEL)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(
            len([item for item in payload["messages"][0]["content"] if item["type"] == "image_url"]),
            3,
        )
        self.assertEqual(payload["reasoning_effort"], "none")

    def test_groq_provider_status_never_overrides_engine_visual_thresholds(self) -> None:
        low = dict(_PASS)
        low["status"] = "pass"
        low["relevance"] = 0.1
        normalized = closure._groq_parse_and_normalize(json.dumps(low))
        self.assertEqual(normalized["status"], "block")


class Run181RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        closure._GROQ_MODEL_CERTIFIED.set(None)

    def test_planning_gemini_429_skips_duplicate_vision_gemini_and_uses_groq_for_long_or_short(self) -> None:
        for _fmt in ("film", "moment"):
            with self.subTest(fmt=_fmt), tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.dict(
                os.environ, {"GEMINI_CONTENT_MODEL": "gemini-3.7-flash"}, clear=False
            ), mock.patch.object(
                closure, "_run_groq_attempt", return_value=dict(_PASS)
            ) as groq, mock.patch.object(
                v2, "_run_openrouter_attempt"
            ) as openrouter:
                health.publish_provider_unavailable(
                    "gemini",
                    model="gemini-3.7-flash",
                    quota_domain=closure.GEMINI_GENERATION_QUOTA_DOMAIN,
                    reason="429 quota from planning",
                    source="planning_provider_loop",
                )
                gemini = mock.Mock(return_value=dict(_PASS))
                result = closure._route_visual_audit_v3(
                    None,
                    _spec(),
                    "gemini",
                    "gemini-3.7-flash",
                    gemini,
                    "gem-key",
                    _preview(root),
                    narration_context="ctx",
                    intended_visual="intent",
                )
            self.assertEqual(result["status"], "pass")
            gemini.assert_not_called()
            groq.assert_called_once()
            openrouter.assert_not_called()
            health.reset_provider_health()

    def test_semantic_block_remains_final_and_never_provider_shops(self) -> None:
        block = dict(_PASS)
        block["status"] = "block"
        with tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.object(
            closure, "_run_groq_attempt"
        ) as groq, mock.patch.object(v2, "_run_openrouter_attempt") as openrouter:
            result = closure._route_visual_audit_v3(
                None,
                _spec(),
                "gemini",
                "gemini-3.7-flash",
                lambda *_args, **_kwargs: block,
                "gem-key",
                _preview(root),
                narration_context="ctx",
                intended_visual="intent",
            )
        self.assertEqual(result["status"], "block")
        groq.assert_not_called()
        openrouter.assert_not_called()

    def test_known_openrouter_preflight_block_does_not_spend_openrouter_attempt(self) -> None:
        health.publish_provider_unavailable(
            "gemini",
            model="gemini-3.7-flash",
            quota_domain=closure.GEMINI_GENERATION_QUOTA_DOMAIN,
            reason="429 quota",
            source="planning",
        )
        health.publish_provider_unavailable(
            "groq",
            model=closure.GROQ_VISION_MODEL,
            quota_domain=closure.GROQ_VISION_QUOTA_DOMAIN,
            reason="vision capacity unavailable",
            source="vision",
        )
        health.publish_provider_unavailable(
            "openrouter",
            model="*",
            quota_domain="*",
            reason="key spend capacity exhausted",
            source="provider_preflight",
        )
        with tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.object(
            v2, "_run_openrouter_attempt"
        ) as openrouter:
            with self.assertRaises(legacy.VisionProviderMeshUnavailableError) as raised:
                closure._route_visual_audit_v3(
                    None,
                    _spec(),
                    "gemini",
                    "gemini-3.7-flash",
                    mock.Mock(),
                    "gem-key",
                    _preview(root),
                    narration_context="ctx",
                    intended_visual="intent",
                )
        openrouter.assert_not_called()
        self.assertIn("spend capacity exhausted", str(raised.exception))

    def test_groq_failure_falls_to_openrouter_without_expanding_three_attempt_cap(self) -> None:
        with tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.object(
            closure,
            "_run_groq_attempt",
            side_effect=v2.VisionStageError(
                v2.VisionErrorCode.PROVIDER_TRANSIENT,
                "Groq 503",
                provider="groq",
                requested_model=closure.GROQ_VISION_MODEL,
            ),
        ) as groq, mock.patch.object(
            v2, "_run_openrouter_attempt", return_value=(dict(_PASS), "resolved/free")
        ) as openrouter:
            result = closure._route_visual_audit_v3(
                None,
                _spec(),
                "gemini",
                "gemini-3.7-flash",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED")),
                "gem-key",
                _preview(root),
                narration_context="ctx",
                intended_visual="intent",
            )
        self.assertEqual(result["status"], "pass")
        groq.assert_called_once()
        openrouter.assert_called_once()

    def test_text_audit_rate_limit_bridge_marks_only_gemini_generation_domain(self) -> None:
        route = SimpleNamespace(
            provider="groq:openai/gpt-oss-120b",
            exhausted=False,
            attempts=[
                SimpleNamespace(provider="gemini", outcome=SimpleNamespace(value="rate_limited"), detail="429 quota"),
                SimpleNamespace(provider="groq:openai/gpt-oss-120b", outcome=SimpleNamespace(value="success"), detail=None),
            ],
        )
        original = text_mesh._record_route_telemetry
        try:
            text_mesh._record_route_telemetry = mock.Mock()
            closure._install_text_audit_health_bridge()
            text_mesh._record_route_telemetry(route)
        finally:
            text_mesh._record_route_telemetry = original
        self.assertIsNotNone(
            health.provider_unavailable(
                "gemini",
                model=closure._gemini_runtime_model(),
                quota_domain=closure.GEMINI_GENERATION_QUOTA_DOMAIN,
            )
        )
        self.assertIsNone(
            health.provider_unavailable(
                "gemini",
                model="gemini-3.1-flash-tts-preview",
                quota_domain="tts",
            )
        )


if __name__ == "__main__":
    unittest.main()
