from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec
from scripts import vision_provider_reliability as legacy
from scripts import vision_stage_contract_v2 as v2


_PASS = {
    "status": "pass",
    "relevance": 0.91,
    "visual_quality": 0.88,
    "identifiable_person": False,
    "sensitive_trait_implication_risk": False,
    "prominent_logo_or_brand": False,
    "cultural_conflict": False,
    "cultural_islamic_suitability_risk": False,
    "advertiser_conflict": False,
    "obvious_synthetic_or_visual_artifact": False,
    "reason": "safe and relevant",
}


def _spec(task_id: str = "VISUAL_AUDIT_S01_C01", *, fmt: str = "moment") -> TaskSpec:
    del fmt
    return TaskSpec(
        task_id=task_id,
        kind="VISUAL_AUDIT",
        priority=Priority.P0,
        capability=Capability.VISION,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=False,
    )


def _preview(temp_dir: str, payload: bytes = b"fake-mp4-preview") -> Path:
    path = Path(temp_dir) / "preview.mp4"
    path.write_bytes(payload)
    return path


class VisionStageContractShapeTests(unittest.TestCase):
    def test_stage_contract_is_explicit_and_shared(self) -> None:
        self.assertEqual(v2.VISION_STAGE_SPEC.stage_id, "vision.visual_audit")
        self.assertEqual(v2.VISION_STAGE_SPEC.contract_id, "vision.visual_audit.v2")
        self.assertEqual(v2.VISION_STAGE_SPEC.provider_policy.providers, ("gemini", "openrouter"))
        self.assertEqual(v2.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts, 3)
        self.assertTrue(v2.VISION_STAGE_SPEC.provider_policy.semantic_block_is_final)

    def test_openrouter_payload_requires_native_strict_schema(self) -> None:
        payload = v2._openrouter_request_payload(
            "openrouter/free",
            prompt="audit",
            frame_items=[{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}}],
        )
        response_format = payload["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertFalse(response_format["json_schema"]["schema"]["additionalProperties"])
        self.assertEqual(set(response_format["json_schema"]["schema"]["required"]), set(_PASS))
        self.assertTrue(payload["provider"]["require_parameters"])
        self.assertTrue(payload["provider"]["allow_fallbacks"])

    def test_schema_rejects_missing_and_extra_fields_with_safe_diagnostics(self) -> None:
        missing = dict(_PASS)
        missing.pop("reason")
        with self.assertRaises(v2.VisionStageError) as raised:
            v2._parse_and_normalize(json.dumps(missing), resolved_model="model-a")
        self.assertEqual(raised.exception.code, v2.VisionErrorCode.STRUCTURAL_INVALID)
        self.assertIn("reason", str(raised.exception))

        extra = dict(_PASS)
        extra["winner"] = True
        with self.assertRaises(v2.VisionStageError) as raised_extra:
            v2._parse_and_normalize(json.dumps(extra), resolved_model="model-a")
        self.assertIn("winner", str(raised_extra.exception))

    def test_schema_rejects_wrong_types_and_score_range(self) -> None:
        wrong = dict(_PASS)
        wrong["advertiser_conflict"] = "false"
        with self.assertRaises(v2.VisionStageError) as raised:
            v2._parse_and_normalize(json.dumps(wrong))
        self.assertIn("advertiser_conflict", str(raised.exception))

        out_of_range = dict(_PASS)
        out_of_range["relevance"] = 1.2
        with self.assertRaises(v2.VisionStageError) as raised_range:
            v2._parse_and_normalize(json.dumps(out_of_range))
        self.assertIn("out of range", str(raised_range.exception))

    def test_provider_status_is_not_authoritative(self) -> None:
        low = dict(_PASS)
        low["status"] = "pass"
        low["relevance"] = 0.2
        normalized = v2._parse_and_normalize(json.dumps(low))
        self.assertEqual(normalized["status"], "block")

    def test_input_hash_binds_preview_context_intent_and_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preview = _preview(temp_dir)
            a = v2.vision_input_hash(preview, narration_context="ctx", intended_visual="intent")
            b = v2.vision_input_hash(preview, narration_context="ctx2", intended_visual="intent")
            c = v2.vision_input_hash(preview, narration_context="ctx", intended_visual="intent2")
            preview.write_bytes(b"changed")
            d = v2.vision_input_hash(preview, narration_context="ctx", intended_visual="intent")
        self.assertEqual(len(a), 64)
        self.assertEqual(len({a, b, c, d}), 4)


class OpenRouterStrictTransportTests(unittest.TestCase):
    def test_request_uses_sampled_images_metadata_and_strict_contract(self) -> None:
        class Response:
            ok = True
            status_code = 200

            def json(self):
                return {
                    "model": "resolved/free-vision-model",
                    "choices": [{"message": {"content": json.dumps(_PASS)}}],
                    "openrouter_metadata": {"strategy": "free", "attempt": 1},
                }

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False
        ), mock.patch.object(
            legacy, "_sample_preview_frames", return_value=[b"a", b"b", b"c"]
        ), mock.patch.object(v2.requests, "post", return_value=Response()) as post:
            audit, resolved = v2._openrouter_call(
                _preview(temp_dir),
                narration_context="ctx",
                intended_visual="intent",
                model="openrouter/free",
            )
        self.assertEqual(audit["status"], "pass")
        self.assertEqual(resolved, "resolved/free-vision-model")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["provider"]["require_parameters"])
        self.assertEqual(post.call_args.kwargs["headers"]["X-OpenRouter-Metadata"], "enabled")
        content = payload["messages"][0]["content"]
        self.assertEqual(len([item for item in content if item["type"] == "image_url"]), 3)

    def test_http_auth_failure_is_fail_fast_class(self) -> None:
        class Response:
            ok = False
            status_code = 401

            def json(self):
                return {"error": {"message": "Unauthorized"}}

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False
        ), mock.patch.object(legacy, "_sample_preview_frames", return_value=[b"a", b"b", b"c"]), mock.patch.object(
            v2.requests, "post", return_value=Response()
        ):
            with self.assertRaises(v2.VisionStageError) as raised:
                v2._openrouter_call(
                    _preview(temp_dir), narration_context="ctx", intended_visual="intent", model="openrouter/free"
                )
        self.assertEqual(raised.exception.code, v2.VisionErrorCode.AUTH_CONFIG)

    def test_no_compatible_endpoint_is_capacity_not_schema(self) -> None:
        class Response:
            ok = False
            status_code = 404

            def json(self):
                return {"error": {"message": "No endpoints found that support response_format"}}

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False
        ), mock.patch.object(legacy, "_sample_preview_frames", return_value=[b"a", b"b", b"c"]), mock.patch.object(
            v2.requests, "post", return_value=Response()
        ):
            with self.assertRaises(v2.VisionStageError) as raised:
                v2._openrouter_call(
                    _preview(temp_dir), narration_context="ctx", intended_visual="intent", model="openrouter/free"
                )
        self.assertEqual(raised.exception.code, v2.VisionErrorCode.CAPACITY)

    def test_catalog_discovery_is_free_vision_structured_and_excludes_first_model(self) -> None:
        class Response:
            ok = True

            def json(self):
                return {
                    "data": [
                        {
                            "id": "model/text:free",
                            "pricing": {"prompt": "0", "completion": "0"},
                            "architecture": {"input_modalities": ["text"]},
                            "supported_parameters": ["response_format"],
                        },
                        {
                            "id": "model/no-json:free",
                            "pricing": {"prompt": "0", "completion": "0"},
                            "architecture": {"input_modalities": ["text", "image"]},
                            "supported_parameters": ["tools"],
                        },
                        {
                            "id": "model/first:free",
                            "pricing": {"prompt": "0", "completion": "0"},
                            "architecture": {"input_modalities": ["image"]},
                            "supported_parameters": ["response_format"],
                        },
                        {
                            "id": "model/alternate:free",
                            "pricing": {"prompt": "0", "completion": "0"},
                            "architecture": {"input_modalities": ["image"]},
                            "supported_parameters": ["response_format"],
                        },
                    ]
                }

        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}, clear=False), mock.patch.object(
            v2.requests, "get", return_value=Response()
        ):
            selected = v2._discover_alternate_free_vision_model(exclude={"model/first:free"})
        self.assertEqual(selected, "model/alternate:free")


class SharedLongShortRoutingTests(unittest.TestCase):
    def test_semantic_block_never_provider_shops(self) -> None:
        block = dict(_PASS)
        block["status"] = "block"
        with tempfile.TemporaryDirectory() as temp_dir, legacy.vision_provider_circuit_scope(), mock.patch.object(
            v2, "_run_openrouter_attempt"
        ) as openrouter:
            result = v2._route_visual_audit_v2(
                None,
                _spec(),
                "gemini",
                "gemini-3.7-flash",
                lambda *_args, **_kwargs: block,
                "gem-key",
                _preview(temp_dir),
                narration_context="ctx",
                intended_visual="intent",
            )
        self.assertEqual(result["status"], "block")
        openrouter.assert_not_called()

    def test_gemini_429_falls_to_openrouter_for_long_and_short(self) -> None:
        for fmt in ("film", "moment"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as temp_dir, legacy.vision_provider_circuit_scope(), mock.patch.object(
                v2, "_run_openrouter_attempt", return_value=(dict(_PASS), "resolved/free")
            ) as openrouter:
                result = v2._route_visual_audit_v2(
                    None,
                    _spec(fmt=fmt),
                    "gemini",
                    "gemini-3.7-flash",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED")),
                    "gem-key",
                    _preview(temp_dir),
                    narration_context="ctx",
                    intended_visual="intent",
                )
            self.assertEqual(result["status"], "pass")
            openrouter.assert_called_once()

    def test_gemini_auth_failure_does_not_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, legacy.vision_provider_circuit_scope(), mock.patch.object(
            v2, "_run_openrouter_attempt"
        ) as openrouter:
            with self.assertRaisesRegex(RuntimeError, "401 unauthorized"):
                v2._route_visual_audit_v2(
                    None,
                    _spec(),
                    "gemini",
                    "gemini-3.7-flash",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("401 unauthorized")),
                    "gem-key",
                    _preview(temp_dir),
                    narration_context="ctx",
                    intended_visual="intent",
                )
        openrouter.assert_not_called()

    def test_schema_invalid_is_model_scoped_then_one_diverse_retry(self) -> None:
        first = v2.VisionStageError(
            v2.VisionErrorCode.STRUCTURAL_INVALID,
            "missing field",
            provider="openrouter",
            requested_model="openrouter/free",
            resolved_model="model/first:free",
        )
        with tempfile.TemporaryDirectory() as temp_dir, legacy.vision_provider_circuit_scope(), mock.patch.object(
            v2,
            "_run_openrouter_attempt",
            side_effect=[first, (dict(_PASS), "model/alternate:free")],
        ) as attempt, mock.patch.object(
            v2, "_discover_alternate_free_vision_model", return_value="model/alternate:free"
        ) as discover:
            result = v2._route_visual_audit_v2(
                None,
                _spec(),
                "gemini",
                "gemini-3.7-flash",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Gemini timeout")),
                "gem-key",
                _preview(temp_dir),
                narration_context="ctx",
                intended_visual="intent",
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(attempt.call_count, 2)
        discover.assert_called_once()
        self.assertEqual(attempt.call_args_list[1].kwargs["requested_model"], "model/alternate:free")

    def test_two_schema_invalid_models_fail_closed_and_open_gateway_circuit(self) -> None:
        first = v2.VisionStageError(
            v2.VisionErrorCode.STRUCTURAL_INVALID,
            "missing field",
            provider="openrouter",
            resolved_model="model/first:free",
        )
        second = v2.VisionStageError(
            v2.VisionErrorCode.STRUCTURAL_INVALID,
            "wrong type",
            provider="openrouter",
            resolved_model="model/alternate:free",
        )
        with tempfile.TemporaryDirectory() as temp_dir, legacy.vision_provider_circuit_scope() as state, mock.patch.object(
            v2, "_run_openrouter_attempt", side_effect=[first, second]
        ), mock.patch.object(v2, "_discover_alternate_free_vision_model", return_value="model/alternate:free"):
            with self.assertRaises(legacy.VisionProviderMeshUnavailableError):
                v2._route_visual_audit_v2(
                    None,
                    _spec(),
                    "gemini",
                    "gemini-3.7-flash",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Gemini timeout")),
                    "gem-key",
                    _preview(temp_dir),
                    narration_context="ctx",
                    intended_visual="intent",
                )
        self.assertTrue(state.gemini_open)
        self.assertTrue(state.openrouter_open)

    def test_schema_invalid_without_diverse_free_model_fails_closed(self) -> None:
        first = v2.VisionStageError(
            v2.VisionErrorCode.STRUCTURAL_INVALID,
            "missing field",
            provider="openrouter",
            resolved_model="only/free:free",
        )
        with tempfile.TemporaryDirectory() as temp_dir, legacy.vision_provider_circuit_scope() as state, mock.patch.object(
            v2, "_run_openrouter_attempt", side_effect=first
        ), mock.patch.object(v2, "_discover_alternate_free_vision_model", return_value=None):
            with self.assertRaises(legacy.VisionProviderMeshUnavailableError):
                v2._route_visual_audit_v2(
                    None,
                    _spec(),
                    "gemini",
                    "gemini-3.7-flash",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Gemini timeout")),
                    "gem-key",
                    _preview(temp_dir),
                    narration_context="ctx",
                    intended_visual="intent",
                )
        self.assertTrue(state.openrouter_open)

    def test_openrouter_transport_failure_is_gateway_scoped(self) -> None:
        failure = v2.VisionStageError(
            v2.VisionErrorCode.PROVIDER_TRANSIENT,
            "HTTP_429 rate limited",
            provider="openrouter",
        )
        with tempfile.TemporaryDirectory() as temp_dir, legacy.vision_provider_circuit_scope() as state, mock.patch.object(
            v2, "_run_openrouter_attempt", side_effect=failure
        ), mock.patch.object(v2, "_discover_alternate_free_vision_model") as discover:
            with self.assertRaises(legacy.VisionProviderMeshUnavailableError):
                v2._route_visual_audit_v2(
                    None,
                    _spec(),
                    "gemini",
                    "gemini-3.7-flash",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Gemini timeout")),
                    "gem-key",
                    _preview(temp_dir),
                    narration_context="ctx",
                    intended_visual="intent",
                )
        self.assertTrue(state.openrouter_open)
        discover.assert_not_called()

    def test_three_real_inference_attempts_are_ledger_visible(self) -> None:
        ledger = BudgetLedger("moment", enforce=True)
        first_schema = v2.VisionStageError(
            v2.VisionErrorCode.STRUCTURAL_INVALID,
            "missing field",
            provider="openrouter",
            requested_model="openrouter/free",
            resolved_model="model/first:free",
        )
        with tempfile.TemporaryDirectory() as temp_dir, legacy.vision_provider_circuit_scope(), mock.patch.object(
            v2,
            "_openrouter_call",
            side_effect=[first_schema, (dict(_PASS), "model/alternate:free")],
        ), mock.patch.object(v2, "_discover_alternate_free_vision_model", return_value="model/alternate:free"):
            result = v2._route_visual_audit_v2(
                ledger,
                _spec(),
                "gemini",
                "gemini-3.7-flash",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("Gemini timeout")),
                "gem-key",
                _preview(temp_dir),
                narration_context="ctx",
                intended_visual="intent",
            )
        self.assertEqual(result["status"], "pass")
        summary = ledger.to_summary()["provider_attempts"]
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_provider"], {"gemini": 1, "openrouter": 2})
        self.assertEqual(ledger._tasks["VISUAL_AUDIT_S01_C01"].max_provider_attempts, 3)

    def test_new_production_scope_resets_both_circuits(self) -> None:
        with legacy.vision_provider_circuit_scope() as first:
            first.gemini_open = True
            first.openrouter_open = True
        with legacy.vision_provider_circuit_scope() as second:
            self.assertFalse(second.gemini_open)
            self.assertFalse(second.openrouter_open)


class InstallerAndDurableBindingTests(unittest.TestCase):
    def test_installer_marks_shared_long_short_v2_owner_and_is_idempotent(self) -> None:
        original_status = v2.orchestrator._ledger_call_status
        original_produce = v2.orchestrator.produce
        try:
            v2.install_vision_provider_reliability()
            status_once = v2.orchestrator._ledger_call_status
            produce_once = v2.orchestrator.produce
            self.assertTrue(getattr(status_once, "_isco_vision_stage_contract_v2", False))
            self.assertTrue(getattr(status_once, "_isco_shared_vision_provider_mesh", False))
            v2.install_vision_provider_reliability()
            self.assertIs(v2.orchestrator._ledger_call_status, status_once)
            self.assertIs(v2.orchestrator.produce, produce_once)
        finally:
            v2.orchestrator._ledger_call_status = original_status
            v2.orchestrator.produce = original_produce

    def test_media_durable_audit_contract_is_bound_to_v2_fingerprint(self) -> None:
        from scripts import media_durable_cache as media_cache

        original = media_cache._audit_contract
        try:
            def legacy_contract():
                return "legacy-contract"

            media_cache._audit_contract = legacy_contract
            v2._bind_media_durable_audit_contract()
            bound = media_cache._audit_contract
            self.assertTrue(getattr(bound, "_isco_vision_stage_contract_v2", False))
            with mock.patch.object(v2, "vision_contract_fingerprint", return_value="v2-fingerprint"), mock.patch.object(
                media_cache, "_contract_hash", side_effect=lambda kind, *parts: kind + ":" + ":".join(parts)
            ):
                value = bound()
            self.assertIn("legacy-contract", value)
            self.assertIn("v2-fingerprint", value)
        finally:
            media_cache._audit_contract = original


if __name__ == "__main__":
    unittest.main()
