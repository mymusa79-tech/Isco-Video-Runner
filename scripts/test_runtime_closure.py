from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import runtime_closure


RUNNER = Path(__file__).with_name("run_v3_voice.py")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class RuntimeClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RUNNER.read_text(encoding="utf-8")
        tree = ast.parse(cls.text)
        cls.main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")

    def test_run71_installer_is_bound_after_append_guard(self) -> None:
        calls=[(node.lineno,_call_name(node)) for node in ast.walk(self.main) if isinstance(node,ast.Call)]
        append=[line for line,name in calls if name=="install_append_retry_guard"]
        closure=[line for line,name in calls if name=="install_runtime_closure"]
        produce=[line for line,name in calls if name=="produce"]
        self.assertEqual(len(append),1); self.assertEqual(len(closure),1); self.assertEqual(len(produce),1)
        self.assertLess(append[0],closure[0]); self.assertLess(closure[0],produce[0])

    def test_g1_g2_runs_once_after_gold_before_manifest(self) -> None:
        calls=[(node.lineno,_call_name(node)) for node in ast.walk(self.main) if isinstance(node,ast.Call)]
        gold=[line for line,name in calls if name=="run_gold_enforce_phase4"]
        observer=[line for line,name in calls if name=="run_post_gold_observers"]
        manifest=[line for line,name in calls if name=="_write_production_manifest"]
        self.assertEqual(len(gold),1); self.assertEqual(len(observer),1); self.assertEqual(len(manifest),1)
        self.assertLess(gold[0],observer[0]); self.assertLess(observer[0],manifest[0])

    def test_post_gold_observer_uses_optional_env_key_and_never_raises(self) -> None:
        with patch.dict(os.environ,{},clear=True), patch.object(runtime_closure,"run_groq_audio_audit",side_effect=RuntimeError("synthetic")) as audit:
            result=runtime_closure.run_post_gold_observers(Path("output/example"))
        audit.assert_called_once_with(Path("output/example"),api_key="")
        self.assertEqual(result["mode"],"observe_only"); self.assertEqual(result["decision"],"audit_error")

    def test_post_gold_observer_reads_existing_secret_file_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key_path=Path(temp_dir)/"groq"; key_path.write_text("secret-token",encoding="utf-8")
            with patch.dict(os.environ,{"GROQ_API_KEY_FILE":str(key_path)},clear=True), patch.object(runtime_closure,"run_groq_audio_audit",return_value={"decision":"pass"}) as audit:
                result=runtime_closure.run_post_gold_observers(Path("output/example"))
            audit.assert_called_once_with(Path("output/example"),api_key="secret-token")
            self.assertEqual(result["decision"],"pass"); self.assertTrue(key_path.exists())

    def test_runtime_closure_installs_cinematic_chain_then_cta_music_then_bundle(self) -> None:
        calls=[]
        with patch.object(runtime_closure,"install_attempt10_append_bound_recovery",side_effect=lambda:calls.append("recovery")) as recovery, \
             patch.object(runtime_closure,"install_audio_mastering_live_binding",side_effect=lambda:calls.append("audio")) as audio, \
             patch.object(runtime_closure,"install_sfx_live_binding",side_effect=lambda:calls.append("sfx")) as sfx, \
             patch.object(runtime_closure,"install_m8_live_binding",side_effect=lambda:calls.append("m8")) as m8, \
             patch.object(runtime_closure,"install_m9_live_binding",side_effect=lambda:calls.append("m9")) as m9, \
             patch.object(runtime_closure,"install_m10_live_binding",side_effect=lambda:calls.append("m10")) as m10, \
             patch.object(runtime_closure,"install_cta_live_binding",side_effect=lambda:calls.append("cta")) as cta, \
             patch.object(runtime_closure,"install_narrative_music_dynamics",side_effect=lambda:calls.append("music")) as music, \
             patch.object(runtime_closure,"install_canonical_v4_bundle_post_manifest",side_effect=lambda:calls.append("bundle")) as bundle:
            runtime_closure.install_runtime_closure()
        recovery.assert_called_once_with(); audio.assert_called_once_with(); sfx.assert_called_once_with()
        m8.assert_called_once_with(); m9.assert_called_once_with(); m10.assert_called_once_with(); cta.assert_called_once_with()
        music.assert_called_once_with(); bundle.assert_called_once_with()
        self.assertEqual(calls,["recovery","audio","sfx","m8","m9","m10","cta","music","bundle"])

    def test_bundle_activation_is_exact_workflow_or_explicit_opt_in(self) -> None:
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(runtime_closure._canonical_v4_bundle_enabled())
        with patch.dict(os.environ,{"ISCO_CANONICAL_V4_BUNDLE_ENABLED":"1"},clear=True):
            self.assertTrue(runtime_closure._canonical_v4_bundle_enabled())
        with patch.dict(os.environ,{
            "GITHUB_EVENT_NAME":"workflow_dispatch",
            "GITHUB_WORKFLOW_REF":"mymusa79-tech/Isco-Video-Runner/.github/workflows/produce-resilient-v4.yml@refs/heads/main",
        },clear=True):
            self.assertTrue(runtime_closure._canonical_v4_bundle_enabled())
        with patch.dict(os.environ,{
            "GITHUB_EVENT_NAME":"pull_request",
            "GITHUB_WORKFLOW_REF":"mymusa79-tech/Isco-Video-Runner/.github/workflows/verify-canonical-v4-bundle-temp.yml@refs/pull/241/merge",
        },clear=True):
            self.assertFalse(runtime_closure._canonical_v4_bundle_enabled())

    def test_manifest_hook_runs_bundle_only_for_canonical_long(self) -> None:
        import scripts.run_v3_voice as production
        original = production._write_production_manifest
        calls=[]
        production._write_production_manifest=lambda out, *, production_id, fmt: calls.append(("manifest",fmt)) or {"format":fmt}
        try:
            with patch("scripts.canonical_v4_bundle.build_canonical_v4_bundle", return_value=Path("delivery-manifest.json")) as build, \
                 patch.object(Path,"is_file",return_value=True), \
                 patch.dict(os.environ,{"ISCO_CANONICAL_V4_BUNDLE_ENABLED":"1"},clear=False):
                os.environ.pop("ISCO_CONTROL_REQUEST_ID",None)
                runtime_closure.install_canonical_v4_bundle_post_manifest()
                result=production._write_production_manifest(Path("output/x"),production_id="p",fmt="film")
                self.assertEqual(result,{"format":"film"})
                build.assert_called_once_with(Path("output/x"))
        finally:
            production._write_production_manifest=original

    def test_manifest_hook_skips_unactivated_long_moment_and_control_plane(self) -> None:
        import scripts.run_v3_voice as production
        original = production._write_production_manifest
        production._write_production_manifest=lambda out, *, production_id, fmt: {"format":fmt}
        try:
            with patch("scripts.canonical_v4_bundle.build_canonical_v4_bundle") as build, patch.dict(os.environ,{
                "GITHUB_EVENT_NAME":"pull_request",
                "GITHUB_WORKFLOW_REF":"mymusa79-tech/Isco-Video-Runner/.github/workflows/verify-canonical-v4-bundle-temp.yml@refs/pull/241/merge",
            },clear=False):
                os.environ.pop("ISCO_CANONICAL_V4_BUNDLE_ENABLED",None)
                os.environ.pop("ISCO_CONTROL_REQUEST_ID",None)
                runtime_closure.install_canonical_v4_bundle_post_manifest()
                production._write_production_manifest(Path("output/generic"),production_id="p",fmt="film")
                with patch.dict(os.environ,{"ISCO_CANONICAL_V4_BUNDLE_ENABLED":"1"},clear=False):
                    production._write_production_manifest(Path("output/moment"),production_id="p",fmt="moment")
                    with patch.dict(os.environ,{"ISCO_CONTROL_REQUEST_ID":"explicit-control"},clear=False):
                        production._write_production_manifest(Path("output/control"),production_id="p",fmt="film")
                build.assert_not_called()
        finally:
            production._write_production_manifest=original


if __name__ == "__main__": unittest.main()
