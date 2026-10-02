from __future__ import annotations

import json
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clean_v2 import pipeline, providers, tone_audit
from clean_v2.deadline import StageDeadlineError, StageDeadlineExceeded, stage_deadline
from isco_video_agent import text_audit_router, tone_quality


def tone_payload(*, blocked=False):
    return {
        "status": "block" if blocked else "pass",
        "preachiness_flags": [], "cultural_dignity_flags": [],
        "naturalness_flags": ["s1: unnatural grammar"] if blocked else [],
        "narrative_format_flags": [], "unverified_religious_quote_flags": [],
        "notes": [], "hook_specificity": True, "hook_honesty": True,
        "hook_curiosity": True, "hook_genericness": False,
        "hook_body_continuity": True, "payoff_resolves_hook": True,
        "section_dependency": True, "topic_fidelity": True,
    }


def hanging_call(_prompt=None):
    # Reproduce a transport that never completes. Ordinary provider error
    # handling must not swallow the stage deadline or advance to another model.
    while True:
        try:
            time.sleep(60)
        except Exception:
            continue


class StageDeadlineTests(unittest.TestCase):
    def test_hung_provider_exits_entire_route_without_fallback(self):
        fallback_calls = []
        started = time.monotonic()
        with self.assertRaises(StageDeadlineExceeded):
            with stage_deadline("text_audit", 0.03):
                text_audit_router.route_text_audit([
                    ("gemini", hanging_call),
                    ("groq", lambda p: fallback_calls.append(p)),
                ], "prompt")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(fallback_calls, [])

    def test_timer_and_handler_restored_after_success_or_error(self):
        handler = signal.getsignal(signal.SIGALRM)
        for fail in (False, True):
            with self.subTest(fail=fail):
                try:
                    with stage_deadline("text_audit", 1):
                        if fail:
                            raise ValueError("provider error")
                except ValueError:
                    self.assertTrue(fail)
                self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_worker_thread_fails_before_provider_work(self):
        errors = []
        work = []

        def worker():
            try:
                with stage_deadline("text_audit", 1):
                    work.append("called")
            except StageDeadlineError as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(work, [])

    def test_existing_timer_is_not_replaced(self):
        handler = signal.getsignal(signal.SIGALRM)
        signal.setitimer(signal.ITIMER_REAL, 10)
        try:
            with self.assertRaises(StageDeadlineError):
                with stage_deadline("text_audit", 1):
                    self.fail("must reject before work")
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 9)
            self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


class TextAuditPipelineDeadlineTests(unittest.TestCase):
    def test_all_formats_record_timeout_as_infrastructure_and_keep_script_checkpoint(self):
        for fmt in ("short", "film", "podcast"):
            with self.subTest(format=fmt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                checkpoint = root / "resume-checkpoint.json"
                checkpoint.write_text('{"completed_stage":"script"}', encoding="utf-8")
                journal = pipeline._Journal(root / "run-manifest.json", runner_sha="runner", engine_sha="engine")
                for name in pipeline.STAGES[:pipeline.STAGES.index("text_audit")]:
                    journal.reuse(name)
                with patch.object(pipeline, "TEXT_AUDIT_DEADLINE_SECONDS", 0.03):
                    with self.assertRaises(StageDeadlineExceeded):
                        journal.run("text_audit", lambda: pipeline._run_text_audit_with_one_bounded_tone_repair(
                            text_audit=lambda **kw: hanging_call(), router=None,
                            output_dir=root, brief={"format": fmt}, plan={}, script={},
                        ))
                manifest = json.loads(journal.path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["failure_classification"], "infrastructure")
                self.assertEqual(manifest["failure_origin_stage"], "text_audit")
                self.assertIsNotNone(manifest["finished_at"])
                failed = manifest["stages"][-1]
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["error_type"], "StageDeadlineExceeded")
                self.assertEqual(failed["deadline_seconds"], 0.03)
                self.assertNotIn("quality_pending_stage", manifest)
                self.assertEqual(json.loads(checkpoint.read_text())["completed_stage"], "script")

    def test_repair_cannot_renew_audit_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_calls = []

            def blocked_audit(**kw):
                audit_calls.append(1)
                time.sleep(0.03)
                raise pipeline.CleanV2ToneContentBlock({"naturalness_flags": ["grammar"]})

            with patch.object(pipeline, "TEXT_AUDIT_DEADLINE_SECONDS", 0.06), patch.object(
                pipeline, "_run_one_bounded_tone_repair", side_effect=lambda **kw: hanging_call()
            ) as repair:
                with self.assertRaises(StageDeadlineExceeded):
                    pipeline._run_text_audit_with_one_bounded_tone_repair(
                        text_audit=blocked_audit, router=None, output_dir=Path(tmp),
                        brief={"format": "short"}, plan={}, script={},
                    )
            self.assertEqual(audit_calls, [1])
            repair.assert_called_once()

    def test_429_circuit_shared_across_audits_and_reset_for_next_run(self):
        calls = []

        def rate_limited(prompt):
            calls.append(("gemini", prompt))
            raise RuntimeError("http_429")

        def available(prompt):
            calls.append(("groq", prompt))
            return {"status": "pass"}

        def audits(**kw):
            for prompt in ("factuality", "tone", "reaudit"):
                result = text_audit_router.route_text_audit([
                    ("gemini", rate_limited), ("groq", available),
                ], prompt)
                self.assertEqual(result.provider, "groq")
            return {"status": "pass"}

        for _ in range(2):
            pipeline._run_text_audit_with_one_bounded_tone_repair(
                text_audit=audits, router=None, output_dir=Path("unused"),
                brief={"format": "short"}, plan={}, script={},
            )
        self.assertEqual([p for name, p in calls if name == "gemini"], ["factuality", "factuality"])
        self.assertEqual(len([c for c in calls if c[0] == "groq"]), 6)


class ToneBoundedTransportTests(unittest.TestCase):
    def setUp(self):
        self.plan = SimpleNamespace(format="short", to_dict=lambda: {"format": "short"})

    def test_tone_uses_bounded_http_schema_on_every_base_provider(self):
        requests = []

        def post(url, **kw):
            requests.append((url, kw))
            if len(requests) < 3:
                raise providers.ProviderWireFailure("connection error")
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(tone_payload())}}]}

        original_route = tone_quality.route_text_audit
        with patch.object(providers, "_read_secret", return_value="test-key"), patch.object(
            providers, "_post_json", side_effect=post
        ), patch.object(tone_quality, "json_text", side_effect=AssertionError("legacy SDK called")), patch.object(
            tone_audit, "_mistral_tone_call", side_effect=AssertionError("extra call")
        ):
            result = tone_audit.audit_tone_and_naturalness_with_mistral("", self.plan, "")
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["provider"], "openrouter")
        self.assertEqual([kw["timeout"] for _, kw in requests], [90, 90, 120])
        self.assertIs(requests[0][1]["payload"]["generationConfig"]["responseJsonSchema"], tone_audit.TONE_AUDIT_SCHEMA)
        for _, kw in requests[1:]:
            contract = kw["payload"]["response_format"]["json_schema"]
            self.assertTrue(contract["strict"])
            self.assertIs(contract["schema"], tone_audit.TONE_AUDIT_SCHEMA)
        self.assertIs(tone_quality.route_text_audit, original_route)

    def test_valid_content_block_stops_without_approval_shopping(self):
        with patch.object(providers, "_gemini_call", return_value=tone_payload(blocked=True)), patch.object(
            providers, "_groq_call", side_effect=AssertionError("block bypassed")
        ), patch.object(providers, "_openrouter_call", side_effect=AssertionError("block bypassed")), patch.object(
            tone_audit, "_mistral_tone_call", side_effect=AssertionError("block bypassed")
        ):
            result = tone_audit.audit_tone_and_naturalness_with_mistral("", self.plan, "")
        self.assertEqual(result["status"], "block")
        self.assertEqual(result["validation"], "valid")
        self.assertEqual(result["naturalness_flags"], ["s1: unnatural grammar"])


if __name__ == "__main__":
    unittest.main()
