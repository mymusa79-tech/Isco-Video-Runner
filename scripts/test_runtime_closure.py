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

    def test_planning_entrypoint_seam_precedes_runtime_closure_and_produce(self) -> None:
        calls=[(node.lineno,_call_name(node)) for node in ast.walk(self.main) if isinstance(node,ast.Call)]
        entrypoint_planning=[line for line,name in calls if name=="install_entrypoint_planning_contracts"]
        closure=[line for line,name in calls if name=="install_runtime_closure"]
        produce=[line for line,name in calls if name=="produce"]
        self.assertEqual(len(entrypoint_planning),1); self.assertEqual(len(closure),1); self.assertEqual(len(produce),1)
        self.assertLess(entrypoint_planning[0],closure[0]); self.assertLess(closure[0],produce[0])

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

    def test_runtime_closure_installs_planning_media_audio_cinematic_and_final_gate_in_order(self) -> None:
        calls=[]
        with patch.object(runtime_closure,"install_runtime_planning_contracts",side_effect=lambda:calls.append("planning")) as planning, \
             patch.object(runtime_closure,"install_media_runtime_port",side_effect=lambda:calls.append("media-port")) as media_port, \
             patch.object(runtime_closure,"install_core_reliability_guard",side_effect=lambda:calls.append("core")) as core, \
             patch.object(runtime_closure,"install_audio_semantic_integrity_binding",side_effect=lambda:calls.append("audio-semantic-binding")) as semantic_binding, \
             patch.object(runtime_closure,"install_audio_mastering_live_binding",side_effect=lambda:calls.append("audio")) as audio, \
             patch.object(runtime_closure,"install_cinematic_runtime_port",side_effect=lambda phase:calls.append("cinematic-inner")) as cinematic, \
             patch.object(runtime_closure,"install_render_durable_cache",side_effect=lambda:calls.append("render-cache")) as render_cache, \
             patch.object(runtime_closure,"install_narrative_music_dynamics",side_effect=lambda:calls.append("music")) as music, \
             patch.object(runtime_closure,"install_canonical_v4_bundle_post_manifest",side_effect=lambda:calls.append("bundle")) as bundle, \
             patch.object(runtime_closure,"install_release_transaction_guard",side_effect=lambda:calls.append("release")) as release, \
             patch.object(runtime_closure,"install_telemetry_reliability_binding",side_effect=lambda:calls.append("telemetry")) as telemetry, \
             patch.object(runtime_closure,"sanitize_final_observer_cache_before_runtime",side_effect=lambda:calls.append("observer-cache-trust")) as observer_cache_trust, \
             patch.object(runtime_closure,"install_final_qc_observer_durability",side_effect=lambda:calls.append("observer-durability")) as observer_durability, \
             patch.object(runtime_closure,"install_audio_semantic_final_gate",side_effect=lambda modules:calls.append("audio-semantic-final")) as semantic_final, \
             patch.object(runtime_closure,"production_entrypoint_modules",return_value=[object()]) as modules, \
             patch.object(runtime_closure,"canonical_runtime_enabled",return_value=False):
            runtime_closure.install_runtime_closure()
        planning.assert_called_once_with(); media_port.assert_called_once_with(); core.assert_called_once_with(); semantic_binding.assert_called_once_with()
        audio.assert_called_once_with(); cinematic.assert_called_once_with(runtime_closure.CinematicInstallPhase.INNER)
        render_cache.assert_called_once_with(); music.assert_called_once_with(); bundle.assert_called_once_with(); release.assert_called_once_with(); telemetry.assert_called_once_with()
        observer_cache_trust.assert_called_once_with(); observer_durability.assert_called_once_with()
        modules.assert_called(); semantic_final.assert_called_once()
        self.assertLess(calls.index("planning"), calls.index("media-port"))
        self.assertLess(calls.index("media-port"), calls.index("core"))
        self.assertLess(calls.index("core"), calls.index("audio-semantic-binding"))
        self.assertLess(calls.index("audio-semantic-binding"), calls.index("audio"))
        self.assertLess(calls.index("audio"), calls.index("cinematic-inner"))
        self.assertLess(calls.index("cinematic-inner"), calls.index("render-cache"))
        self.assertEqual(calls[-1], "observer-durability")

    def test_stable_ports_and_audio_semantic_order_around_render(self) -> None:
        source = Path(runtime_closure.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        install = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "install_runtime_closure")
        calls=[(node.lineno,_call_name(node)) for node in ast.walk(install) if isinstance(node,ast.Call)]
        planning_line=next(line for line,name in calls if name=="install_runtime_planning_contracts")
        media_line=next(line for line,name in calls if name=="install_media_runtime_port")
        core_line=next(line for line,name in calls if name=="install_core_reliability_guard")
        semantic_line=next(line for line,name in calls if name=="install_audio_semantic_integrity_binding")
        audio_line=next(line for line,name in calls if name=="install_audio_mastering_live_binding")
        cinematic_line=next(line for line,name in calls if name=="install_cinematic_runtime_port")
        render_line=next(line for line,name in calls if name=="install_render_durable_cache")
        final_line=next(line for line,name in calls if name=="install_audio_semantic_final_gate")
        telemetry_line=next(line for line,name in calls if name=="install_telemetry_reliability_binding")
        self.assertLess(planning_line,media_line)
        self.assertLess(media_line,core_line)
        self.assertLess(core_line,semantic_line)
        self.assertLess(semantic_line,audio_line)
        self.assertLess(audio_line,cinematic_line)
        self.assertLess(cinematic_line,render_line)
        self.assertLess(telemetry_line,final_line)

    def test_bundle_activation_requires_live_runtime_or_explicit_test_opt_in(self) -> None:
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(runtime_closure._canonical_v4_bundle_enabled())
        with patch.dict(os.environ,{"ISCO_CANONICAL_V4_BUNDLE_ENABLED":"1"},clear=True):
            self.assertTrue(runtime_closure._canonical_v4_bundle_enabled())
        with patch.dict(os.environ,{
            "GITHUB_ACTIONS":"true",
            "GITHUB_EVENT_NAME":"workflow_dispatch",
            "GITHUB_WORKFLOW_REF":"mymusa79-tech/Isco-Video-Runner/.github/workflows/produce-resilient-v4.yml@refs/heads/main",
        },clear=True):
            self.assertFalse(runtime_closure._canonical_v4_bundle_enabled())
        with patch.dict(os.environ,{
            "GITHUB_ACTIONS":"true",
            "GITHUB_EVENT_NAME":"workflow_dispatch",
            "GITHUB_WORKFLOW_REF":"mymusa79-tech/Isco-Video-Runner/.github/workflows/produce-resilient-v4.yml@refs/heads/main",
            "ISCO_CANONICAL_RUNTIME":"1",
        },clear=True):
            self.assertTrue(runtime_closure._canonical_v4_bundle_enabled())
        with patch.dict(os.environ,{
            "GITHUB_ACTIONS":"true",
            "GITHUB_EVENT_NAME":"pull_request",
            "GITHUB_WORKFLOW_REF":"mymusa79-tech/Isco-Video-Runner/.github/workflows/verify-canonical-v4-bundle-temp.yml@refs/pull/241/merge",
            "ISCO_CANONICAL_RUNTIME":"1",
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
