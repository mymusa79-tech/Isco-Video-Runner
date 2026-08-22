from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path

from scripts import groq_audio_audit as audit


class _Response:
    status = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class GroqAudioAuditTests(unittest.TestCase):
    def test_g1_allows_only_explicit_free_only_models(self):
        for model in audit.FREE_ONLY_MODELS:
            self.assertEqual(audit.assert_free_only_model(model), model)
        with self.assertRaises(audit.GroqFreeOnlyViolation):
            audit.assert_free_only_model("paid/future-model")

    def test_g1_audio_path_rejects_non_whisper_allowed_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "a.flac"
            audio_path.write_bytes(b"audio")
            with self.assertRaises(audit.GroqFreeOnlyViolation):
                audit.transcribe_audio(
                    audio_path,
                    api_key="x",
                    model="openai/gpt-oss-20b",
                    urlopen=lambda *args, **kwargs: _Response({"text": "ignored"}),
                )

    def test_g1_429_is_fail_open_no_paid_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "plan.json").write_text(
                json.dumps({"sections": [{"narration": "هذا اختبار واضح"}]}), encoding="utf-8"
            )
            (out / "final.mp4").write_bytes(b"video")

            def extractor(_video: Path, audio_path: Path):
                audio_path.write_bytes(b"audio")

            def rate_limited(*args, **kwargs):
                raise audit.GroqRateLimited("groq_audio_rate_limited_429")

            result = audit.run_groq_audio_audit(
                out,
                api_key="x",
                extractor=extractor,
                transcriber=rate_limited,
            )
            self.assertEqual(result["decision"], "audit_skipped")
            self.assertEqual(result["groq_governor"]["http_status"], 429)
            self.assertFalse(result["groq_governor"]["paid_fallback_enabled"])
            self.assertFalse(result["groq_governor"]["auto_upgrade_enabled"])

    def test_normalize_arabic_removes_diacritics_and_alef_variants(self):
        self.assertEqual(audit.normalize_arabic("إِنَّ الآمَالَ"), "ان الامال")

    def test_narration_from_plan_joins_sections_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(
                json.dumps(
                    {"sections": [{"id": "s1", "narration": "الأول"}, {"id": "s2", "narration": "الثاني"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(audit.narration_from_plan(path), "الأول\nالثاني")

    def test_compare_exact_transcript_passes(self):
        result = audit.compare_transcripts("هذا نص عربي واضح للاختبار", "هذا نص عربي واضح للاختبار")
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["token_recall"], 1.0)
        self.assertEqual(result["char_similarity"], 1.0)

    def test_compare_large_omission_requires_review(self):
        result = audit.compare_transcripts(
            "واحد اثنان ثلاثة اربعة خمسة ستة سبعة ثمانية تسعة عشرة",
            "واحد اثنان ثلاثة",
        )
        self.assertEqual(result["decision"], "review")
        self.assertLess(result["token_recall"], audit.PASS_TOKEN_RECALL)

    def test_transcribe_posts_arabic_whisper_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "voice.flac"
            audio_path.write_bytes(b"abc")
            captured = {}

            def opener(request, timeout):
                captured["url"] = request.full_url
                captured["auth"] = request.headers.get("Authorization")
                captured["body"] = request.data
                captured["timeout"] = timeout
                return _Response({"text": "نص مسموع"})

            text, telemetry = audit.transcribe_audio(audio_path, api_key="secret", urlopen=opener)
            self.assertEqual(text, "نص مسموع")
            self.assertEqual(telemetry["status"], "ok")
            self.assertEqual(captured["url"], audit.GROQ_TRANSCRIPTION_ENDPOINT)
            self.assertEqual(captured["auth"], "Bearer secret")
            self.assertIn(b"whisper-large-v3-turbo", captured["body"])
            self.assertIn(b"\r\n\r\nar\r\n", captured["body"])

    def test_http_429_never_retries_or_switches_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "voice.flac"
            audio_path.write_bytes(b"abc")
            calls = []

            def opener(request, timeout):
                calls.append(request.data)
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    hdrs=None,
                    fp=BytesIO(b'{"error":"rate limit"}'),
                )

            with self.assertRaises(audit.GroqRateLimited):
                audit.transcribe_audio(audio_path, api_key="secret", urlopen=opener)
            self.assertEqual(len(calls), 1)
            self.assertIn(audit.DEFAULT_AUDIO_MODEL.encode(), calls[0])

    def test_g2_writes_observe_only_audit_and_does_not_modify_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            plan_path = out / "plan.json"
            final_path = out / "final.mp4"
            plan_bytes = json.dumps(
                {"sections": [{"narration": "هذا النص النهائي يجب أن يسمع كما هو"}]},
                ensure_ascii=False,
            ).encode("utf-8")
            final_bytes = b"immutable-final-video"
            plan_path.write_bytes(plan_bytes)
            final_path.write_bytes(final_bytes)

            def extractor(_video: Path, audio_path: Path):
                audio_path.write_bytes(b"audio")

            def transcriber(_audio: Path, *, api_key: str, model: str):
                return (
                    "هذا النص النهائي يجب أن يسمع كما هو",
                    {
                        "policy": "free_only",
                        "model": model,
                        "allowed": True,
                        "status": "ok",
                        "http_status": 200,
                        "auto_upgrade_enabled": False,
                        "paid_fallback_enabled": False,
                        "rate_limit_action": None,
                    },
                )

            result = audit.run_groq_audio_audit(
                out,
                api_key="secret",
                extractor=extractor,
                transcriber=transcriber,
            )
            self.assertEqual(result["decision"], "pass")
            self.assertEqual(result["mode"], "observe_only")
            self.assertEqual(result["enforcement"], "disabled")
            self.assertEqual(plan_path.read_bytes(), plan_bytes)
            self.assertEqual(final_path.read_bytes(), final_bytes)
            saved = json.loads((out / audit.AUDIT_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(saved["decision"], "pass")

    def test_g2_missing_key_becomes_audit_error_not_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "plan.json").write_text(
                json.dumps({"sections": [{"narration": "نص"}]}, ensure_ascii=False), encoding="utf-8"
            )
            (out / "final.mp4").write_bytes(b"video")

            def extractor(_video: Path, audio_path: Path):
                audio_path.write_bytes(b"audio")

            result = audit.run_groq_audio_audit(out, api_key=None, extractor=extractor)
            self.assertEqual(result["decision"], "audit_error")
            self.assertIn("groq_api_key_missing", result["audit_error"])
            self.assertTrue((out / audit.AUDIT_FILENAME).is_file())


if __name__ == "__main__":
    unittest.main()
