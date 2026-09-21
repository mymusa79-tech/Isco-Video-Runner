from __future__ import annotations

import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from clean_v2.media import (
    AzureF0NeuralVoiceSynthesizer,
    GeminiPrimaryPiperFallbackSynthesizer,
    TtsProviderError,
    VoiceInfrastructureError,
)
from clean_v2.pipeline import STAGES, _Journal, _synthesize_sectioned_voice


def _write_audio(path: Path, marker: bytes = b"G") -> Path:
    path.write_bytes(marker * 2048)
    return path


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 2048)
    return buffer.getvalue()


class _RateLimit(RuntimeError):
    http_status = 429
    retry_after_seconds = 0.25


class _LongRateLimit(RuntimeError):
    http_status = 429
    retry_after_seconds = 30.0


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


class CleanV2VoiceRoutingTests(unittest.TestCase):
    def _synthesizer(
        self,
        root: Path,
        *,
        gemini_key: str = "gemini-test-key",
        azure_key: str = "",
        azure_region: str = "",
        azure_confirmed: bool = False,
        azure_voice_approved: bool = False,
        allow_piper: bool = False,
    ) -> GeminiPrimaryPiperFallbackSynthesizer:
        return GeminiPrimaryPiperFallbackSynthesizer(
            gemini_key,
            root / "ar_JO-kareem-medium.onnx",
            None,
            tts_model="gemini-3.1-flash-tts-preview",
            azure_api_key=azure_key,
            azure_region=azure_region,
            azure_free_tier_confirmed=azure_confirmed,
            azure_voice_approved=azure_voice_approved,
            allow_piper_fallback=allow_piper,
        )

    def test_charon_is_attempted_first_and_piper_is_not_used_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "narration.wav"
            synth = self._synthesizer(root)
            events: list[str] = []

            def gemini(*args, **kwargs):
                events.append("gemini")
                self.assertEqual(kwargs["voice"], "Charon")
                self.assertEqual(kwargs["model"], "gemini-3.1-flash-tts-preview")
                return _write_audio(Path(args[2]))

            def piper(*_args, **_kwargs):
                events.append("piper")
                raise AssertionError("Piper must not run after successful Charon")

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize", side_effect=gemini), \
                 patch.object(synth.piper, "synthesize", side_effect=piper):
                result = synth.synthesize("هذا اختبار للصوت الأساسي.", output)

            self.assertEqual(result, output)
            self.assertEqual(events, ["gemini"])
            self.assertEqual(synth.charon_attempts, 1)
            self.assertEqual(synth.last_provider, "gemini:Charon")
            self.assertFalse(synth.fallback_used)
            self.assertEqual(synth.voice_approval_status, "human_approved_reference")
            self.assertEqual(
                synth.voice_reference_profile, "channel-voice-roster-v1"
            )

    def test_sectioned_charon_retries_only_the_failing_section_and_keeps_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "narration.wav"
            (root / "narration.txt").write_text(
                "authoritative transcript\n", encoding="utf-8"
            )
            synth = self._synthesizer(root)
            calls: list[str] = []
            s2_attempts = 0

            def gemini(*args, **_kwargs):
                nonlocal s2_attempts
                transcript = str(args[1])
                target = Path(args[2])
                calls.append(transcript)
                if transcript == "القسم الثاني.":
                    s2_attempts += 1
                    if s2_attempts < 3:
                        raise RuntimeError(
                            "Gemini TTS failed after retries: "
                            "{'type': 'APITimeoutError'}"
                        )
                return _write_audio(target)

            def concat_audio(inputs, target):
                self.assertEqual(
                    [path.name for path in inputs],
                    ["01.wav", "02.wav", "03.wav"],
                )
                return _write_audio(Path(target), b"J")

            sections = [
                {"id": "s1", "narration": "القسم الأول."},
                {"id": "s2", "narration": "القسم الثاني."},
                {"id": "s3", "narration": "القسم الثالث."},
            ]

            with patch(
                "clean_v2.media._legacy_voice_identity",
                return_value=("Charon", "Orus"),
            ), patch(
                "clean_v2.media._legacy_gemini_synthesize",
                side_effect=gemini,
            ), patch(
                "clean_v2.media.time.sleep"
            ) as sleep, patch(
                "clean_v2.pipeline.concat_wav_parts",
                side_effect=concat_audio,
            ):
                result = _synthesize_sectioned_voice(synth, sections, output)

            self.assertEqual(
                calls,
                [
                    "القسم الأول.",
                    "القسم الثاني.",
                    "القسم الثاني.",
                    "القسم الثاني.",
                    "القسم الثالث.",
                ],
            )
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                [1.0, 2.0],
            )
            self.assertEqual(result["voice_provider"], "gemini:Charon")
            self.assertEqual(result["charon_tts_attempts"], 5)
            self.assertFalse(result["voice_fallback_used"])
            self.assertTrue(output.is_file())
            self.assertTrue((root / "audio" / "01.wav").is_file())
            self.assertTrue((root / "audio" / "02.wav").is_file())
            self.assertTrue((root / "audio" / "03.wav").is_file())
            self.assertEqual(
                (root / "narration.txt").read_text(encoding="utf-8"),
                "authoritative transcript\n",
            )
            report = json.loads(
                (root / "voice-sections.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                [item["charon_attempts"] for item in report["sections"]],
                [1, 3, 1],
            )

    def test_charon_retries_three_total_attempts_before_any_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "narration.wav"
            synth = self._synthesizer(root)
            events: list[str] = []

            def gemini(*args, **_kwargs):
                events.append("gemini")
                if len(events) < 3:
                    raise RuntimeError("temporary Gemini TTS failure")
                return _write_audio(Path(args[2]))

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize", side_effect=gemini), \
                 patch("clean_v2.media.time.sleep") as sleep, \
                 patch.object(synth.azure, "synthesize") as azure, \
                 patch.object(synth.piper, "synthesize") as piper:
                result = synth.synthesize("محاولة صوت فصيح طبيعية.", output)

            self.assertEqual(result, output)
            self.assertEqual(events, ["gemini", "gemini", "gemini"])
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])
            azure.assert_not_called()
            piper.assert_not_called()
            self.assertEqual(synth.last_provider, "gemini:Charon")

    def test_charon_honors_short_retry_after_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "narration.wav"
            synth = self._synthesizer(root)
            attempts = 0

            def gemini(*args, **_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise _RateLimit("rate limited")
                return _write_audio(Path(args[2]))

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize", side_effect=gemini), \
                 patch("clean_v2.media.time.sleep") as sleep:
                synth.synthesize("اختبار انتظار مزود الصوت.", output)

            self.assertEqual(attempts, 2)
            sleep.assert_called_once_with(0.25)

    def test_long_retry_after_is_not_shortened_before_neural_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "narration.wav"
            synth = self._synthesizer(
                root,
                azure_key="azure-test-key",
                azure_region="uaenorth",
                azure_confirmed=True,
                azure_voice_approved=True,
            )
            events: list[str] = []

            def gemini(*_args, **_kwargs):
                events.append("charon")
                raise _LongRateLimit("Retry-After=30")

            def azure(_transcript: str, target: Path) -> Path:
                events.append("azure")
                return _write_audio(target, b"A")

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize", side_effect=gemini), \
                 patch("clean_v2.media.time.sleep") as sleep, \
                 patch.object(synth.azure, "synthesize", side_effect=azure), \
                 patch.object(synth.piper, "synthesize") as piper:
                result = synth.synthesize("احترام نافذة انتظار مزود الصوت.", output)

            self.assertEqual(result, output)
            self.assertEqual(events, ["charon", "azure"])
            self.assertEqual(synth.charon_attempts, 1)
            sleep.assert_not_called()
            piper.assert_not_called()

    def test_persistent_charon_failure_is_infrastructure_and_never_silent_piper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = self._synthesizer(root)

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch(
                     "clean_v2.media._legacy_gemini_synthesize",
                     side_effect=RuntimeError("synthetic Gemini TTS failure"),
                 ) as gemini, \
                 patch("clean_v2.media.time.sleep"), \
                 patch.object(synth.piper, "synthesize") as piper:
                with self.assertRaisesRegex(
                    VoiceInfrastructureError, "CLEAN_V2_VOICE_INFRASTRUCTURE"
                ) as raised:
                    synth.synthesize("هذا اختبار للفشل المغلق.", root / "narration.wav")

            self.assertEqual(gemini.call_count, 3)
            self.assertEqual(raised.exception.charon_attempts, 3)
            self.assertFalse(raised.exception.piper_fallback_allowed)
            piper.assert_not_called()
            self.assertIsNone(synth.last_provider)

    def test_charon_failure_reason_carries_the_real_message_not_just_the_type(
        self,
    ) -> None:
        # Engine's synthesize_wav wraps every underlying TTS failure (auth,
        # quota, network, model access...) in a generic RuntimeError, so
        # type(exc).__name__ alone is always "RuntimeError" regardless of
        # the real cause - charon_reason must also carry str(exc), which is
        # where Engine's own safe_error() detail actually lives.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = self._synthesizer(root)

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch(
                     "clean_v2.media._legacy_gemini_synthesize",
                     side_effect=RuntimeError(
                         "Gemini TTS failed after retries: "
                         "{'type': 'PermissionDenied', 'status_code': 403}"
                     ),
                 ), \
                 patch("clean_v2.media.time.sleep"), \
                 patch.object(synth.piper, "synthesize") as piper:
                with self.assertRaises(VoiceInfrastructureError) as raised:
                    synth.synthesize("هذا اختبار لرسالة الفشل الحقيقية.", root / "narration.wav")

            self.assertIn("PermissionDenied", raised.exception.charon_reason)
            self.assertIn("PermissionDenied", str(raised.exception))
            piper.assert_not_called()

    def test_azure_f0_neural_runs_only_after_all_charon_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "narration.wav"
            synth = self._synthesizer(
                root,
                azure_key="azure-test-key",
                azure_region="uaenorth",
                azure_confirmed=True,
                azure_voice_approved=True,
            )
            events: list[str] = []

            def gemini(*_args, **_kwargs):
                events.append("charon")
                raise RuntimeError("Charon unavailable")

            def azure(_transcript: str, target: Path) -> Path:
                events.append("azure")
                return _write_audio(target, b"A")

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize", side_effect=gemini), \
                 patch("clean_v2.media.time.sleep"), \
                 patch.object(synth.azure, "synthesize", side_effect=azure), \
                 patch.object(synth.piper, "synthesize") as piper:
                result = synth.synthesize("نص عربي فصيح واضح وطبيعي.", output)

            self.assertEqual(result, output)
            self.assertEqual(events, ["charon", "charon", "charon", "azure"])
            piper.assert_not_called()
            self.assertEqual(synth.last_provider, "azure-f0:ar-OM-AbdullahNeural")
            self.assertTrue(synth.fallback_used)
            self.assertEqual(synth.voice_approval_status, "human_approved_fallback")
            self.assertEqual(
                synth.voice_reference_profile,
                "azure-f0:ar-OM-AbdullahNeural",
            )

    def test_azure_f0_is_disabled_until_free_tier_is_explicitly_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = self._synthesizer(
                root,
                azure_key="azure-test-key",
                azure_region="uaenorth",
                azure_confirmed=False,
            )

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch(
                     "clean_v2.media._legacy_gemini_synthesize",
                     side_effect=RuntimeError("Charon unavailable"),
                 ) as gemini, \
                 patch("clean_v2.media.time.sleep"), \
                 patch.object(synth.azure, "synthesize") as azure, \
                 patch.object(synth.piper, "synthesize") as piper:
                with self.assertRaises(VoiceInfrastructureError) as raised:
                    synth.synthesize("لا نستخدم حصة مدفوعة ضمنيًا.", root / "narration.wav")

            self.assertEqual(gemini.call_count, 3)
            self.assertEqual(
                raised.exception.secondary_reason, "azure_f0_not_confirmed"
            )
            azure.assert_not_called()
            piper.assert_not_called()

    def test_azure_f0_is_disabled_until_voice_sample_is_human_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = self._synthesizer(
                root,
                azure_key="azure-test-key",
                azure_region="uaenorth",
                azure_confirmed=True,
                azure_voice_approved=False,
            )

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch(
                     "clean_v2.media._legacy_gemini_synthesize",
                     side_effect=RuntimeError("Charon unavailable"),
                 ) as gemini, \
                 patch("clean_v2.media.time.sleep"), \
                 patch.object(synth.azure, "synthesize") as azure, \
                 patch.object(synth.piper, "synthesize") as piper:
                with self.assertRaises(VoiceInfrastructureError) as raised:
                    synth.synthesize("الصوت المرشح لا يدخل الإنتاج قبل سماعه.", root / "narration.wav")

            self.assertEqual(gemini.call_count, 3)
            self.assertEqual(
                raised.exception.secondary_reason,
                "azure_f0_voice_not_human_approved",
            )
            azure.assert_not_called()
            piper.assert_not_called()

    def test_piper_requires_explicit_emergency_flag_and_cloud_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "narration.wav"
            synth = self._synthesizer(
                root,
                azure_key="azure-test-key",
                azure_region="uaenorth",
                azure_confirmed=True,
                azure_voice_approved=True,
                allow_piper=True,
            )
            events: list[str] = []

            def gemini(*_args, **_kwargs):
                events.append("charon")
                raise RuntimeError("Charon unavailable")

            def azure(*_args, **_kwargs):
                events.append("azure")
                raise TtsProviderError("azure_f0_http_429", http_status=429)

            def piper(_transcript: str, target: Path) -> Path:
                events.append("piper")
                return _write_audio(target, b"P")

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize", side_effect=gemini), \
                 patch("clean_v2.media.time.sleep"), \
                 patch.object(synth.azure, "synthesize", side_effect=azure), \
                 patch.object(synth.piper, "synthesize", side_effect=piper):
                result = synth.synthesize("شبكة الأمان اليدوية فقط.", output)

            self.assertEqual(result, output)
            self.assertEqual(
                events, ["charon", "charon", "charon", "azure", "piper"]
            )
            self.assertEqual(synth.last_provider, "piper-local:ar_JO-kareem-medium")
            self.assertTrue(synth.fallback_used)
            self.assertEqual(
                synth.voice_approval_status,
                "emergency_only_not_naturalness_approved",
            )
            self.assertIsNone(synth.voice_reference_profile)

    def test_emergency_piper_failure_remains_clear_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = self._synthesizer(root, allow_piper=True)

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch(
                     "clean_v2.media._legacy_gemini_synthesize",
                     side_effect=RuntimeError("Charon unavailable"),
                 ) as gemini, \
                 patch("clean_v2.media.time.sleep"), \
                 patch.object(
                     synth.piper,
                     "synthesize",
                     side_effect=RuntimeError("Piper unavailable"),
                 ) as piper:
                with self.assertRaisesRegex(
                    VoiceInfrastructureError, "CLEAN_V2_VOICE_INFRASTRUCTURE"
                ) as raised:
                    synth.synthesize(
                        "حتى الملاذ الأخير يفشل بوضوح.", root / "narration.wav"
                    )

            self.assertEqual(gemini.call_count, 3)
            piper.assert_called_once()
            self.assertIn("piper_RuntimeError", raised.exception.secondary_reason)

    def test_azure_f0_sends_exact_fusha_text_without_dialect_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "azure.wav"
            provider = AzureF0NeuralVoiceSynthesizer(
                "azure-test-key",
                "uaenorth",
                free_tier_confirmed=True,
                voice_approved=True,
            )
            transcript = "لماذا تفشل خطط إدارة الوقت في الحياة اليومية؟"

            with patch(
                "clean_v2.media.urllib.request.urlopen",
                return_value=_FakeResponse(_wav_bytes()),
            ) as urlopen:
                result = provider.synthesize(transcript, output)

            self.assertEqual(result, output)
            request = urlopen.call_args.args[0]
            ssml = request.data.decode("utf-8")
            self.assertIn(transcript, ssml)
            self.assertIn('xml:lang="ar-OM"', ssml)
            self.assertIn('name="ar-OM-AbdullahNeural"', ssml)

    def test_charon_and_orus_match_the_human_approved_reference(self) -> None:
        profile = json.loads(
            Path("voice-profiles/voice-reference-profile-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(profile["mode"], "human_approved_reference")
        self.assertEqual(
            profile["source"]["tts_model"], "gemini-3.1-flash-tts-preview"
        )
        self.assertEqual(
            profile["source"]["human_approval"],
            "Charon=primary; Orus=questioner",
        )
        self.assertEqual(profile["profiles"]["primary"]["voice_name"], "Charon")
        self.assertEqual(
            profile["profiles"]["questioner"]["voice_name"], "Orus"
        )

    def test_fallback_acceptance_workflow_requires_manual_human_approval(self) -> None:
        workflow = Path(
            ".github/workflows/voice-fallback-acceptance.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn('"human_approval_required": True', workflow)
        self.assertIn("CLEAN_V2_AZURE_TTS_VOICE_APPROVED=true", workflow)
        self.assertIn("no audible local dialect", workflow)
        self.assertNotIn("PiperVoiceSynthesizer", workflow)

    def test_narrative_voice_roles_keep_charon_and_add_orus_only_for_dialogue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            single = self._synthesizer(root)
            dialogue = self._synthesizer(root)

            def gemini(*args, **_kwargs):
                return _write_audio(Path(args[2]))

            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize", side_effect=gemini):
                single.synthesize(
                    "سرد مباشر وقصة وتحليل وحوار داخلي بصوت القناة نفسه.",
                    root / "single.wav",
                )
                dialogue.synthesize(
                    "A: لماذا يحدث هذا؟\nB: لأننا نخلط بين الخطة والحياة.",
                    root / "dialogue.wav",
                )

            self.assertEqual(
                single.voice_roles,
                {"mode": "single_narrator", "narrator": "Charon"},
            )
            self.assertEqual(
                dialogue.voice_roles,
                {
                    "mode": "dialogue_qa",
                    "questioner": "Orus",
                    "responder": "Charon",
                },
            )

    def test_voice_identity_mismatch_fails_closed_before_any_tts_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = self._synthesizer(root)
            with patch("clean_v2.media._legacy_voice_identity", return_value=("Gacrux", "Orus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize") as gemini, \
                 patch.object(synth.azure, "synthesize") as azure, \
                 patch.object(synth.piper, "synthesize") as piper:
                with self.assertRaisesRegex(RuntimeError, "primary voice identity mismatch"):
                    synth.synthesize("اختبار هوية الصوت.", root / "narration.wav")
            gemini.assert_not_called()
            azure.assert_not_called()
            piper.assert_not_called()

    def test_questioner_voice_identity_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = self._synthesizer(root)
            with patch("clean_v2.media._legacy_voice_identity", return_value=("Charon", "Iapetus")), \
                 patch("clean_v2.media._legacy_gemini_synthesize") as gemini:
                with self.assertRaisesRegex(RuntimeError, "questioner voice identity mismatch"):
                    synth.synthesize(
                        "A: سؤال.\nB: جواب واضح.", root / "narration.wav"
                    )
            gemini.assert_not_called()

    def test_tts_model_drift_from_human_reference_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            synth = GeminiPrimaryPiperFallbackSynthesizer(
                "gemini-test-key",
                root / "ar_JO-kareem-medium.onnx",
                tts_model="unapproved-tts-model",
            )
            with patch(
                "clean_v2.media._legacy_voice_identity",
                return_value=("Charon", "Orus"),
            ), patch("clean_v2.media._legacy_gemini_synthesize") as gemini:
                with self.assertRaisesRegex(
                    RuntimeError, "human-approved voice reference mismatch"
                ):
                    synth.synthesize(
                        "اختبار تثبيت نموذج الصوت.", root / "narration.wav"
                    )
            gemini.assert_not_called()

    def test_voice_terminal_failure_is_recorded_as_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = _Journal(
                Path(temporary) / "run-manifest.json",
                runner_sha="b" * 40,
                engine_sha="a" * 40,
            )
            for stage in STAGES:
                if stage == "voice":
                    break
                journal.run(stage, lambda: None)

            failure = VoiceInfrastructureError(
                charon_attempts=3,
                charon_reason="RuntimeError",
                secondary_reason="azure_f0_not_configured",
                piper_fallback_allowed=False,
            )
            with self.assertRaises(VoiceInfrastructureError):
                journal.run("voice", lambda: (_ for _ in ()).throw(failure))

            self.assertEqual(journal.payload["status"], "failed")
            self.assertEqual(journal.payload["failure_classification"], "infrastructure")
            self.assertNotIn("quality_pending_stage", journal.payload)
            self.assertEqual(journal.payload["voice_failure"]["charon_attempts"], 3)
            self.assertEqual(
                journal.payload["voice_failure"]["secondary_reason"],
                "azure_f0_not_configured",
            )
            self.assertFalse(
                journal.payload["voice_failure"]["piper_emergency_enabled"]
            )
            self.assertEqual(journal.payload["stages"][-1]["name"], "voice")
            self.assertEqual(
                journal.payload["stages"][-1]["failure_classification"],
                "infrastructure",
            )


if __name__ == "__main__":
    unittest.main()
