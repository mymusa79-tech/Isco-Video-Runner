from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from isco_video_agent.ai_budget import Capability, Priority, TaskSpec
from scripts import canonical_visual_evidence_v1 as canonical
from scripts import mistral_visual_qa_fallback as mistral
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as mesh
from scripts import vision_provider_reliability as legacy
from scripts import vision_stage_contract_v2 as contract


_PASS = {
    "status": "pass",
    "relevance": 0.93,
    "visual_quality": 0.91,
    "identifiable_person": False,
    "sensitive_trait_implication_risk": False,
    "prominent_logo_or_brand": False,
    "cultural_conflict": False,
    "cultural_islamic_suitability_risk": False,
    "advertiser_conflict": False,
    "obvious_synthetic_or_visual_artifact": False,
    "reason": "safe and relevant",
}


def _spec(*, kind: str = "VISUAL_AUDIT") -> TaskSpec:
    return TaskSpec(
        task_id="MISTRAL_VISUAL_QA_FALLBACK",
        kind=kind,
        priority=Priority.P0,
        capability=Capability.VISION,
        max_provider_attempts=5,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


def _evidence(root: str) -> canonical.CanonicalVisualEvidence:
    base = Path(root)
    source = base / "selected-original.mp4"
    source.write_bytes(b"selected-original")
    frames: list[Path] = []
    frame_hashes: list[str] = []
    for index, value in enumerate((b"canonical-one", b"canonical-two", b"canonical-three"), start=1):
        path = base / f"frame-{index:02d}.jpg"
        path.write_bytes(value)
        frames.append(path)
        frame_hashes.append(hashlib.sha256(value).hexdigest())
    prompt = canonical.canonical_visual_prompt(
        narration_context="actual narration context",
        intended_visual="actual intended visual",
    )
    return canonical.CanonicalVisualEvidence(
        source_path=source,
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        frame_paths=tuple(frames),
        frame_sha256=tuple(frame_hashes),
        prompt=prompt,
        prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )


class _Response:
    ok = True
    status_code = 200
    headers = {
        "x-ratelimit-limit-req-minute": "30",
        "x-ratelimit-limit-tokens-minute": "937500",
        "x-ratelimit-remaining-tokens-minute": "932100",
    }

    def __init__(self, audit: dict) -> None:
        self.audit = audit

    def json(self):
        return {
            "model": mistral.MISTRAL_VISION_MODEL,
            "choices": [{"message": {"content": json.dumps(self.audit)}}],
            "usage": {
                "prompt_tokens": 5200,
                "completion_tokens": 200,
                "total_tokens": 5400,
            },
        }


class _RateLimitedResponse:
    ok = False
    status_code = 429
    headers = {
        "Retry-After": "7",
        "x-ratelimit-limit-req-minute": "30",
        "x-ratelimit-limit-tokens-minute": "937500",
    }

    def json(self):
        return {"error": {"message": "rate limit exceeded"}}


class MistralVisualQATransportTests(unittest.TestCase):
    def setUp(self) -> None:
        mistral.reset_mistral_visual_qa_telemetry()

    def test_exact_canonical_frames_prompt_schema_headers_usage_and_normalizer(self) -> None:
        provider_pass = dict(_PASS)
        provider_pass["relevance"] = 0.10
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-key"},
            clear=False,
        ), mock.patch.object(
            mistral.requests,
            "post",
            return_value=_Response(provider_pass),
        ) as post:
            evidence = _evidence(root)
            result = mistral._mistral_visual_call(
                evidence.source_path,
                narration_context="ignored",
                intended_visual="ignored",
                canonical_visual_evidence=evidence,
            )

        # The existing Engine normalizer remains authoritative over provider status.
        self.assertEqual(result["status"], "block")
        self.assertIsNone(
            contract._schema_error(result, resolved_model=mistral.MISTRAL_VISION_MODEL)
        )
        self.assertEqual(post.call_args.args[0], mistral.MISTRAL_CHAT_URL)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "ministral-14b-2512")
        self.assertEqual(payload["response_format"], contract._strict_response_format())
        self.assertFalse(payload["response_format"]["json_schema"]["schema"]["additionalProperties"])
        content = payload["messages"][0]["content"]
        self.assertEqual(
            [item["type"] for item in content],
            ["image_url", "image_url", "image_url", "text"],
        )
        decoded = [
            base64.b64decode(item["image_url"].split(",", 1)[1])
            for item in content[:-1]
        ]
        self.assertEqual(decoded, list(evidence.frame_bytes()))
        self.assertEqual(content[-1]["text"], evidence.prompt)

        telemetry = mistral.get_mistral_visual_qa_telemetry()
        self.assertEqual(len(telemetry), 1)
        self.assertEqual(telemetry[0]["usage"]["total_tokens"], 5400)
        self.assertEqual(
            telemetry[0]["rate_limit_headers"]["x-ratelimit-limit-tokens-minute"],
            "937500",
        )

    def test_retry_after_header_is_preserved_as_exact_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-key"},
            clear=False,
        ), mock.patch.object(
            mistral.requests,
            "post",
            return_value=_RateLimitedResponse(),
        ):
            evidence = _evidence(root)
            with self.assertRaises(contract.VisionStageError) as raised:
                mistral._mistral_visual_call(
                    evidence.source_path,
                    narration_context="ignored",
                    intended_visual="ignored",
                    canonical_visual_evidence=evidence,
                )

        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(mistral.latest_retry_after_seconds(), 7.0)
        telemetry = mistral.get_mistral_visual_qa_telemetry()
        self.assertEqual(telemetry[-1]["rate_limit_headers"]["retry-after"], "7")

    def test_schema_mismatch_is_structural_and_fail_closed(self) -> None:
        malformed = dict(_PASS)
        malformed.pop("reason")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-key"},
            clear=False,
        ), mock.patch.object(
            mistral.requests,
            "post",
            return_value=_Response(malformed),
        ):
            evidence = _evidence(root)
            with self.assertRaises(contract.VisionStageError) as raised:
                mistral._mistral_visual_call(
                    evidence.source_path,
                    narration_context="ignored",
                    intended_visual="ignored",
                    canonical_visual_evidence=evidence,
                )

        self.assertIs(raised.exception.code, contract.VisionErrorCode.STRUCTURAL_INVALID)
        self.assertEqual(raised.exception.provider, mistral.MISTRAL_VISION_PROVIDER)

    def test_non_visual_task_is_rejected_before_any_wire_call(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            mistral.requests,
            "post",
        ) as post:
            evidence = _evidence(root)
            with self.assertRaises(contract.VisionStageError) as raised:
                mistral.run_mistral_visual_qa_attempt(
                    None,
                    _spec(kind="SCRIPT"),
                    preview=evidence.source_path,
                    narration_context="ctx",
                    intended_visual="intent",
                    canonical_visual_evidence=evidence,
                )
        self.assertIs(raised.exception.code, contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR)
        post.assert_not_called()


class MistralVisualQARoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        mesh._GROQ_MODEL_CERTIFIED.set(None)

    def tearDown(self) -> None:
        health.reset_provider_health()
        mesh._GROQ_MODEL_CERTIFIED.set(None)

    @staticmethod
    def _technical(provider: str) -> contract.VisionStageError:
        return contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
            f"{provider} technical capacity failure",
            provider=provider,
        )

    def test_four_technical_failures_reach_mistral_in_exact_order(self) -> None:
        order: list[str] = []

        def gemini(*_args, **_kwargs):
            order.append("gemini")
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

        def technical(name: str):
            def fail(*_args, **_kwargs):
                order.append(name)
                raise self._technical(name)

            return fail

        def mistral_pass(*_args, **_kwargs):
            order.append("mistral")
            return dict(_PASS)

        with tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.object(
            mesh,
            "refresh_runtime_provider_health",
            return_value=None,
        ), mock.patch.object(
            mesh,
            "_run_groq_attempt",
            side_effect=technical("groq"),
        ), mock.patch.object(
            contract,
            "_run_openrouter_attempt",
            side_effect=technical("openrouter"),
        ), mock.patch.object(
            mesh.cloudflare_vision,
            "shared_vision_configured",
            return_value=True,
        ), mock.patch.object(
            mesh,
            "_run_cloudflare_attempt",
            side_effect=technical("cloudflare"),
        ), mock.patch.object(
            mesh.mistral_vision,
            "mistral_visual_configured",
            return_value=True,
        ), mock.patch.object(
            mesh,
            "_run_mistral_attempt",
            side_effect=mistral_pass,
        ):
            evidence = _evidence(root)
            result = mesh._route_visual_audit_v3(
                None,
                _spec(),
                "gemini",
                "gemini-3.7-flash",
                gemini,
                "gem-key",
                evidence.source_path,
                narration_context="ctx",
                intended_visual="intent",
                canonical_evidence=evidence,
            )

        self.assertEqual(
            order,
            ["gemini", "groq", "openrouter", "cloudflare", "mistral"],
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["vision_provider"], "mistral")
        self.assertEqual(result["resolved_model"], "ministral-14b-2512")
        self.assertEqual(
            contract.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts,
            5,
        )

    def test_active_mistral_health_block_skips_wire_attempt(self) -> None:
        health.publish_provider_failure(
            mistral.MISTRAL_VISION_PROVIDER,
            model=mistral.MISTRAL_VISION_MODEL,
            quota_domain=mistral.MISTRAL_VISION_QUOTA_DOMAIN,
            reason="HTTP_429 rate limit exceeded",
            source="vision_stage",
            failure_class=health.FAILURE_RATE_LIMITED,
            retry_after_seconds=30.0,
        )
        with tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.object(
            mesh.mistral_vision,
            "mistral_visual_configured",
            return_value=True,
        ), mock.patch.object(
            mesh,
            "_record_circuit_open",
        ), mock.patch.object(
            mesh,
            "_run_mistral_attempt",
        ) as attempt:
            evidence = _evidence(root)
            with self.assertRaises(legacy.VisionProviderMeshUnavailableError):
                mesh._mistral_or_mesh(
                    None,
                    _spec(),
                    legacy._state(),
                    attempts=4,
                    max_attempts=5,
                    preview=evidence.source_path,
                    narration_context="ctx",
                    intended_visual="intent",
                    input_hash="a" * 64,
                    canonical_visual_evidence=evidence,
                )
        attempt.assert_not_called()

    def test_mistral_retry_after_is_published_to_health_registry(self) -> None:
        error = contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
            "HTTP_429 message=rate limit exceeded",
            provider=mistral.MISTRAL_VISION_PROVIDER,
            requested_model=mistral.MISTRAL_VISION_MODEL,
            http_status=429,
        )
        before = time.monotonic()
        with tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.object(
            mesh.mistral_vision,
            "mistral_visual_configured",
            return_value=True,
        ), mock.patch.object(
            mesh.mistral_vision,
            "latest_retry_after_seconds",
            return_value=7.0,
        ), mock.patch.object(
            mesh,
            "_run_mistral_attempt",
            side_effect=error,
        ):
            evidence = _evidence(root)
            with self.assertRaises(legacy.VisionProviderMeshUnavailableError):
                mesh._mistral_or_mesh(
                    None,
                    _spec(),
                    legacy._state(),
                    attempts=4,
                    max_attempts=5,
                    preview=evidence.source_path,
                    narration_context="ctx",
                    intended_visual="intent",
                    input_hash="b" * 64,
                    canonical_visual_evidence=evidence,
                )
        rows = [
            row
            for row in health.snapshot_provider_health()
            if row["provider"] == mistral.MISTRAL_VISION_PROVIDER
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["failure_class"], health.FAILURE_RATE_LIMITED)
        self.assertIsNotNone(rows[0]["retry_at"])
        self.assertGreaterEqual(rows[0]["retry_at"], before + 6.5)
        self.assertLessEqual(rows[0]["retry_at"], before + 8.5)

    def test_cloudflare_semantic_block_is_final_and_never_calls_mistral(self) -> None:
        block = dict(_PASS)
        block["status"] = "block"
        with tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.object(
            mesh,
            "refresh_runtime_provider_health",
            return_value=None,
        ), mock.patch.object(
            mesh,
            "_run_groq_attempt",
            side_effect=self._technical("groq"),
        ), mock.patch.object(
            contract,
            "_run_openrouter_attempt",
            side_effect=self._technical("openrouter"),
        ), mock.patch.object(
            mesh.cloudflare_vision,
            "shared_vision_configured",
            return_value=True,
        ), mock.patch.object(
            mesh,
            "_run_cloudflare_attempt",
            return_value=block,
        ), mock.patch.object(
            mesh,
            "_run_mistral_attempt",
        ) as mistral_attempt:
            evidence = _evidence(root)
            result = mesh._route_visual_audit_v3(
                None,
                _spec(),
                "gemini",
                "gemini-3.7-flash",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("429 RESOURCE_EXHAUSTED")
                ),
                "gem-key",
                evidence.source_path,
                narration_context="ctx",
                intended_visual="intent",
                canonical_evidence=evidence,
            )

        self.assertEqual(result["status"], "block")
        mistral_attempt.assert_not_called()

    def test_mistral_schema_mismatch_remains_fail_closed_with_no_sixth_route(self) -> None:
        schema_error = contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "schema fields mismatch missing=['reason'] extra=[]",
            provider="mistral",
            requested_model=mistral.MISTRAL_VISION_MODEL,
            resolved_model=mistral.MISTRAL_VISION_MODEL,
        )
        with tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.object(
            mesh,
            "refresh_runtime_provider_health",
            return_value=None,
        ), mock.patch.object(
            mesh,
            "_run_groq_attempt",
            side_effect=self._technical("groq"),
        ), mock.patch.object(
            contract,
            "_run_openrouter_attempt",
            side_effect=self._technical("openrouter"),
        ), mock.patch.object(
            mesh.cloudflare_vision,
            "shared_vision_configured",
            return_value=True,
        ), mock.patch.object(
            mesh,
            "_run_cloudflare_attempt",
            side_effect=self._technical("cloudflare"),
        ), mock.patch.object(
            mesh.mistral_vision,
            "mistral_visual_configured",
            return_value=True,
        ), mock.patch.object(
            mesh,
            "_run_mistral_attempt",
            side_effect=schema_error,
        ):
            evidence = _evidence(root)
            with self.assertRaises(contract.VisionStageError) as raised:
                mesh._route_visual_audit_v3(
                    None,
                    _spec(),
                    "gemini",
                    "gemini-3.7-flash",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        RuntimeError("429 RESOURCE_EXHAUSTED")
                    ),
                    "gem-key",
                    evidence.source_path,
                    narration_context="ctx",
                    intended_visual="intent",
                    canonical_evidence=evidence,
                )

        self.assertIs(raised.exception, schema_error)
        self.assertIs(raised.exception.code, contract.VisionErrorCode.STRUCTURAL_INVALID)


if __name__ == "__main__":
    unittest.main()
