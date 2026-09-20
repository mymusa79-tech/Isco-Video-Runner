from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import canonical_visual_evidence_v1 as evidence
from scripts import gold_cloudflare_vision_fallback as cloudflare
from scripts import mistral_visual_qa_fallback as mistral
from scripts import run181_vision_mesh_closure as mesh
from scripts import vision_stage_contract_v2 as contract


_PASS = {
    "status": "pass",
    "relevance": 0.91,
    "visual_quality": 0.90,
    "identifiable_person": False,
    "sensitive_trait_implication_risk": False,
    "prominent_logo_or_brand": False,
    "cultural_conflict": False,
    "cultural_islamic_suitability_risk": False,
    "advertiser_conflict": False,
    "obvious_synthetic_or_visual_artifact": False,
    "reason": "canonical evidence",
}


def _manual_evidence(root: str) -> evidence.CanonicalVisualEvidence:
    base = Path(root)
    source = base / "original.mp4"
    source.write_bytes(b"original-selected-video")
    frames = []
    hashes = []
    for index, payload in enumerate((b"frame-one-high-quality", b"frame-two-high-quality", b"frame-three-high-quality"), start=1):
        path = base / f"frame-{index:02d}.jpg"
        path.write_bytes(payload)
        frames.append(path)
        hashes.append(hashlib.sha256(payload).hexdigest())
    prompt = evidence.canonical_visual_prompt(
        narration_context="Specific narration context",
        intended_visual="Specific intended visual",
    )
    return evidence.CanonicalVisualEvidence(
        source_path=source,
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        frame_paths=tuple(frames),
        frame_sha256=tuple(hashes),
        prompt=prompt,
        prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    )


def _decode_openai_content(content: list[dict]) -> tuple[list[bytes], str]:
    frames: list[bytes] = []
    prompt = ""
    for item in content:
        if item.get("type") == "image_url":
            image_url = item["image_url"]
            uri = image_url["url"] if isinstance(image_url, dict) else image_url
            frames.append(base64.b64decode(uri.split(",", 1)[1]))
        elif item.get("type") == "text":
            prompt = item["text"]
    return frames, prompt


class CanonicalVisualEvidenceTests(unittest.TestCase):
    def test_prompt_contains_distinctive_semantic_job_rules_once_for_all_providers(self) -> None:
        prompt = evidence.canonical_visual_prompt(
            narration_context="ctx",
            intended_visual="intent",
        )
        self.assertIn("DISTINCTIVE SEMANTIC JOB", prompt)
        self.assertIn("broad mood/theme", prompt)
        self.assertIn("original-source frames", prompt)

    def test_bundle_is_extracted_once_from_original_source_at_fixed_positions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "selected-original.mp4"
            source.write_bytes(b"real-source")
            calls: list[tuple[Path, float]] = []

            def fake_extract(src: Path, dest: Path, timestamp: float) -> None:
                calls.append((Path(src), timestamp))
                dest.write_bytes(f"frame@{timestamp:.6f}".encode("ascii"))

            with mock.patch.object(evidence, "_duration", return_value=10.0), mock.patch.object(
                evidence, "_extract_frame", side_effect=fake_extract
            ):
                bundle = evidence.build_canonical_visual_evidence(
                    source,
                    Path(root) / "bundle",
                    narration_context="ctx",
                    intended_visual="intent",
                )

        self.assertEqual([src for src, _ in calls], [source, source, source])
        self.assertEqual([round(ts, 3) for _, ts in calls], [1.8, 5.0, 8.2])
        self.assertEqual(len(bundle.frame_sha256), 3)
        self.assertTrue(all(len(value) == 64 for value in bundle.frame_sha256))
        self.assertEqual(len(bundle.prompt_hash), 64)

    def test_all_five_providers_receive_same_frame_bytes_same_order_and_same_prompt(self) -> None:
        class OpenAIResponse:
            ok = True
            status_code = 200
            headers = {}
            def json(self):
                return {
                    "model": contract.OPENROUTER_PRIMARY_MODEL,
                    "choices": [{"message": {"content": json.dumps(_PASS)}}],
                }

        class CloudflareResponse:
            ok = True
            status_code = 200
            def json(self):
                return {"success": True, "result": {"response": json.dumps(_PASS)}}

        class Interaction:
            output_text = json.dumps(_PASS)

        class Interactions:
            def __init__(self):
                self.input = None
            def create(self, **kwargs):
                self.input = kwargs["input"]
                return Interaction()

        class Client:
            def __init__(self):
                self.interactions = Interactions()

        with tempfile.TemporaryDirectory() as root:
            bundle = _manual_evidence(root)
            expected_frames = list(bundle.frame_bytes())

            with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "key"}, clear=False), mock.patch.object(
                contract.requests, "post", return_value=OpenAIResponse()
            ) as openrouter_post:
                contract._openrouter_call(
                    bundle.source_path,
                    narration_context="ignored",
                    intended_visual="ignored",
                    model=contract.OPENROUTER_PRIMARY_MODEL,
                    canonical_visual_evidence=bundle,
                )

            with mock.patch.object(mesh, "_certify_groq_vision_model"), mock.patch.object(
                mesh, "_groq_key", return_value="key"
            ), mock.patch.object(mesh.requests, "post", return_value=OpenAIResponse()) as groq_post:
                mesh._groq_visual_call(
                    bundle.source_path,
                    narration_context="ignored",
                    intended_visual="ignored",
                    canonical_visual_evidence=bundle,
                )

            with mock.patch.object(
                cloudflare.requests, "post", return_value=CloudflareResponse()
            ) as cloudflare_post:
                cloudflare._wire_call(
                    "token",
                    "a" * 32,
                    bundle.source_path,
                    narration_context="ignored",
                    intended_visual="ignored",
                    canonical_visual_evidence=bundle,
                )

            mistral.reset_mistral_visual_qa_telemetry()
            with mock.patch.dict("os.environ", {"MISTRAL_API_KEY": "key"}, clear=False), mock.patch.object(
                mistral.requests, "post", return_value=OpenAIResponse()
            ) as mistral_post:
                mistral._mistral_visual_call(
                    bundle.source_path,
                    narration_context="ignored",
                    intended_visual="ignored",
                    canonical_visual_evidence=bundle,
                )

            client = Client()
            with mock.patch.object(
                evidence.gemini_provider, "_client", return_value=client
            ), mock.patch.object(
                evidence.gemini_provider, "_content_model", side_effect=lambda model: model
            ):
                evidence.audit_gemini_canonical_evidence(
                    "key",
                    bundle.source_path,
                    canonical_evidence=bundle,
                    narration_context="ignored",
                    intended_visual="ignored",
                    model="gemini-3.7-flash",
                )

            or_content = openrouter_post.call_args.kwargs["json"]["messages"][0]["content"]
            groq_content = groq_post.call_args.kwargs["json"]["messages"][0]["content"]
            cf_content = cloudflare_post.call_args.kwargs["json"]["messages"][0]["content"]
            mistral_content = mistral_post.call_args.kwargs["json"]["messages"][0]["content"]
            or_frames, or_prompt = _decode_openai_content(or_content)
            groq_frames, groq_prompt = _decode_openai_content(groq_content)
            cf_frames, cf_prompt = _decode_openai_content(cf_content)
            mistral_frames, mistral_prompt = _decode_openai_content(mistral_content)

            gemini_frames = [
                base64.b64decode(item["data"])
                for item in client.interactions.input
                if item["type"] == "image"
            ]
            gemini_prompt = [
                item["text"] for item in client.interactions.input if item["type"] == "text"
            ][0]

        self.assertEqual(or_frames, expected_frames)
        self.assertEqual(groq_frames, expected_frames)
        self.assertEqual(cf_frames, expected_frames)
        self.assertEqual(mistral_frames, expected_frames)
        self.assertEqual(gemini_frames, expected_frames)
        self.assertEqual(or_frames, groq_frames)
        self.assertEqual(or_frames, cf_frames)
        self.assertEqual(or_frames, mistral_frames)
        self.assertEqual(or_frames, gemini_frames)
        self.assertEqual(or_prompt, bundle.prompt)
        self.assertEqual(groq_prompt, bundle.prompt)
        self.assertEqual(cf_prompt, bundle.prompt)
        self.assertEqual(mistral_prompt, bundle.prompt)
        self.assertEqual(gemini_prompt, bundle.prompt)
        self.assertEqual([item["type"] for item in or_content], ["image_url", "image_url", "image_url", "text"])
        self.assertEqual([item["type"] for item in groq_content], ["image_url", "image_url", "image_url", "text"])
        self.assertEqual([item["type"] for item in cf_content], ["image_url", "image_url", "image_url", "text"])
        self.assertEqual([item["type"] for item in mistral_content], ["image_url", "image_url", "image_url", "text"])
        self.assertEqual([item["type"] for item in client.interactions.input], ["image", "image", "image", "text"])

    def test_openrouter_judge_identity_is_fixed_and_not_free_router(self) -> None:
        self.assertEqual(contract.OPENROUTER_PRIMARY_MODEL, "google/gemma-4-26b-a4b-it:free")
        self.assertNotEqual(contract.OPENROUTER_PRIMARY_MODEL, "openrouter/free")

    def test_provenance_records_full_hashes_and_exact_judge(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            bundle = _manual_evidence(root)
            audit = evidence.attach_provenance(
                _PASS,
                provider="cloudflare_workers_ai",
                resolved_model="@cf/meta/llama-4-scout-17b-16e-instruct",
                evidence=bundle,
            )
        self.assertEqual(audit["vision_provider"], "cloudflare_workers_ai")
        self.assertEqual(
            audit["resolved_model"],
            "@cf/meta/llama-4-scout-17b-16e-instruct",
        )
        self.assertEqual(audit["prompt_hash"], bundle.prompt_hash)
        self.assertEqual(audit["frame_sha256"], list(bundle.frame_sha256))
        self.assertTrue(all(len(value) == 64 for value in audit["frame_sha256"]))
        self.assertEqual(len(audit["prompt_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
