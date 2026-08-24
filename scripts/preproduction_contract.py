from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


PRODUCTION_WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")
EXPECTED_ENGINE_SHA = "cae51d6c83262de0a785e0d805462e9392909754"
EXPECTED_RUNNER_IMAGE = "ubuntu-24.04"
PROVIDERS = ("gemini", "groq", "openrouter", "pexels", "pixabay")


@dataclass(frozen=True)
class ContractIssue:
    code: str
    detail: str


def audit_preproduction_contract(repo: Path) -> list[ContractIssue]:
    text = (repo / PRODUCTION_WORKFLOW).read_text(encoding="utf-8")
    issues: list[ContractIssue] = []

    def require(code: str, marker: str, detail: str) -> None:
        if marker not in text:
            issues.append(ContractIssue(code, detail))

    require("runner_image", f"runs-on: {EXPECTED_RUNNER_IMAGE}", "production runner image must be explicit, not a moving -latest alias")
    require("runner_sha_verify", 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', "exact Runner checkout must be verified before execution")
    require("engine_pin", EXPECTED_ENGINE_SHA, "canonical production Engine SHA missing")
    require("post_piper_pip_check", 'python -m pip check # post-piper-certification', "dependency graph must be rechecked after Piper installation")
    require("provider_preflight", "python scripts/provider_preflight.py", "all required providers must be preflighted before production")
    for provider in PROVIDERS:
        require(f"provider_{provider}", f"--{provider}-key-file", f"provider preflight is missing {provider}")
    require("release_namespace_guard", "Release namespace preflight", "non-idempotent GitHub Release tag must be checked before provider work")
    require("release_collision_fail_closed", "existing release tag blocks this run before production", "existing release tag must fail closed rather than overwrite/retry")
    require("production_entrypoint", "python ../scripts/run_v3_voice.py", "canonical production entrypoint changed unexpectedly")
    require("manual_publish", "manual_in_youtube_studio", "manual YouTube publication contract missing")
    require("cleanup", "Remove plaintext production secrets and state", "plaintext secret cleanup step missing")

    # Prevent accidental fallback to an old/deprecated Pexels endpoint in the workflow.
    if "api.pexels.com/videos/" in text:
        issues.append(ContractIssue("deprecated_pexels_endpoint", "deprecated Pexels video endpoint present"))

    # A Production workflow must remain dispatch-only. Other trigger types can create
    # unreviewed side effects simply by pushing code.
    trigger_prefix = text.split("concurrency:", 1)[0]
    if "workflow_dispatch:" not in trigger_prefix or re.search(r"(?m)^\s{2}(push|pull_request|schedule|workflow_run):", trigger_prefix):
        issues.append(ContractIssue("production_trigger", "production workflow must remain workflow_dispatch-only"))

    return issues


def assert_preproduction_contract(repo: Path) -> None:
    issues = audit_preproduction_contract(repo)
    if issues:
        joined = "; ".join(f"{item.code}: {item.detail}" for item in issues)
        raise RuntimeError("Pre-production contract failed: " + joined)


if __name__ == "__main__":
    assert_preproduction_contract(Path("."))
    print("Pre-production static contract PASS")
