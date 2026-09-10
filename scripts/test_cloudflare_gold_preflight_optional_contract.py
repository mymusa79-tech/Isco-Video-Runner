from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("cloudflare_gold_preflight.py")
PROVIDER_PREFLIGHT_PATH = Path(__file__).with_name("provider_preflight.py")


def _load_preflight_with_stubbed_fallback(monkeypatch: pytest.MonkeyPatch):
    fallback = types.ModuleType("scripts.gold_cloudflare_vision_fallback")

    class CloudflareGoldVisionUnavailable(RuntimeError):
        pass

    fallback.CloudflareGoldVisionUnavailable = CloudflareGoldVisionUnavailable
    fallback._credentials = lambda: ("token", "0" * 32)
    fallback._enabled = lambda: True
    fallback._prove_model_access = lambda *_args, **_kwargs: None
    fallback._prove_workers_free = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "scripts.gold_cloudflare_vision_fallback", fallback)

    module_name = "_cloudflare_gold_preflight_optional_contract_under_test"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, CloudflareGoldVisionUnavailable


def test_optional_cloudflare_unavailable_disables_only_that_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, unavailable = _load_preflight_with_stubbed_fallback(monkeypatch)
    github_env = tmp_path / "github-env"
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    monkeypatch.setenv("CLOUDFLARE_GOLD_VISION_FREE_ONLY", "true")

    def fail_strict_probe() -> None:
        raise unavailable("Billing Read missing")

    monkeypatch.setattr(module, "preflight_gold_cloudflare_from_runner_temp", fail_strict_probe)

    assert module.preflight_optional_gold_cloudflare_from_runner_temp() is False
    assert module.os.environ["CLOUDFLARE_GOLD_VISION_FREE_ONLY"] == "false"
    assert github_env.read_text(encoding="utf-8") == (
        "CLOUDFLARE_GOLD_VISION_FREE_ONLY=false\n"
    )
    output = capsys.readouterr().out
    assert "OPTIONAL_UNAVAILABLE" in output
    assert "production continues without Cloudflare Gold fallback" in output


def test_optional_cloudflare_success_remains_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _ = _load_preflight_with_stubbed_fallback(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_GOLD_VISION_FREE_ONLY", "true")
    monkeypatch.delenv("GITHUB_ENV", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "preflight_gold_cloudflare_from_runner_temp",
        lambda: calls.append("strict-probe"),
    )

    assert module.preflight_optional_gold_cloudflare_from_runner_temp() is True
    assert calls == ["strict-probe"]
    assert module.os.environ["CLOUDFLARE_GOLD_VISION_FREE_ONLY"] == "true"


def test_optional_cloudflare_does_not_swallow_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _ = _load_preflight_with_stubbed_fallback(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_GOLD_VISION_FREE_ONLY", "true")

    def programming_bug() -> None:
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(module, "preflight_gold_cloudflare_from_runner_temp", programming_bug)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        module.preflight_optional_gold_cloudflare_from_runner_temp()


def test_provider_preflight_wires_optional_probe_not_strict_probe() -> None:
    tree = ast.parse(PROVIDER_PREFLIGHT_PATH.read_text(encoding="utf-8"))
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "preflight_optional_gold_cloudflare_from_runner_temp" in called_names
    assert "preflight_gold_cloudflare_from_runner_temp" not in called_names
