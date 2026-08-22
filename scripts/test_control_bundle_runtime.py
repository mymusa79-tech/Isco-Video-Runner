from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_control_production as control


class ControlBundleRuntimeTests(unittest.TestCase):
    def _child(self) -> dict:
        request = {
            "schema_version": 1,
            "request_id": "req-parent-s1",
            "source": "telegram_editorial_control_panel",
            "kind": "short",
            "approval_scope": "short_sibling",
            "approved_by_user": True,
            "approved_at": "2026-08-22T12:00:00Z",
            "approved_topic": "زاوية مستقلة",
            "format": "moment",
            "weekly_option_id": "telegram:s:1:s1",
            "content_boundaries": [],
            "approved_research_pack": [],
            "short_admission": {
                "knowledge_gap_score": 9.0,
                "reframe_score": 8.5,
                "immediate_action_score": 8.0,
                "short_fit_score": 8.5,
                "single_action_contract": "ابدأ بخطوة واحدة صغيرة اليوم",
            },
            "parent_control_request_id": "req-parent",
            "parent_control_request_sha256": "parent-sha",
            "source_long_topic": "موضوع الحلقة",
            "source_semantic_job": "زاوية مستقلة",
            "sibling_index": 1,
            "sibling_count": 2,
            "production_dispatch_authorized": False,
            "status": "approved_waiting_production_activation",
            "youtube_publish_mode": "manual_in_youtube_studio",
        }
        request["request_sha256"] = control._canonical_request_hash(request)
        return request

    def test_child_runtime_uses_clean_subprocess_and_exact_hashed_request(self):
        child = self._child()
        with tempfile.TemporaryDirectory() as temp, patch.object(control, "_output_dirs", return_value=set()), patch.object(
            control, "_new_output_dir", return_value=Path(temp) / "output-short"
        ), patch.object(control.subprocess, "run") as run:
            result = control.execute_child_subprocess(child, runtime_root=Path(temp) / "runtime")

        self.assertEqual(result, Path(temp) / "output-short")
        run.assert_called_once()
        command = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(command[0], control.sys.executable)
        self.assertTrue(command[1].endswith("run_control_production.py"))
        self.assertTrue(kwargs["check"])
        env = kwargs["env"]
        self.assertEqual(env["CONTROL_PLANE_PRODUCTION_ENABLED"], "true")
        self.assertEqual(env["ISCO_CONTROL_REQUEST_SHA256"], child["request_sha256"])
        request_path = Path(env["ISCO_CONTROL_REQUEST_PATH"])
        stored = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertFalse(stored["production_dispatch_authorized"])
        self.assertEqual(stored["request_sha256"], child["request_sha256"])

    def test_child_runtime_rejects_persisted_dispatch_authority_before_subprocess(self):
        child = self._child()
        child["production_dispatch_authorized"] = True
        child["request_sha256"] = control._canonical_request_hash(child)
        with tempfile.TemporaryDirectory() as temp, patch.object(control.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "must remain non-dispatching"):
                control.execute_child_subprocess(child, runtime_root=Path(temp))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
