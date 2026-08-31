from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


PRODUCTION_WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")
RELEASE_TRANSACTION = Path("scripts/release_transaction.py")
ENVIRONMENT_PREFLIGHT = Path("scripts/environment_preflight.py")
ENVIRONMENT_PREFLIGHT_CORE = Path("scripts/environment_preflight_core.py")
EXPECTED_ENGINE_SHA = "fe576d91f604412a010fa6cd61ff66f839e67550"
EXPECTED_RUNNER_IMAGE = "ubuntu-24.04"
PROVIDERS = ("gemini", "groq", "openrouter", "pexels", "pixabay")


@dataclass(frozen=True)
class ContractIssue:
    code: str
    detail: str


def _read_if_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def audit_preproduction_contract(repo: Path) -> list[ContractIssue]:
    text = (repo / PRODUCTION_WORKFLOW).read_text(encoding="utf-8")
    release_text = _read_if_file(repo / RELEASE_TRANSACTION)
    environment_wrapper_text = _read_if_file(repo / ENVIRONMENT_PREFLIGHT)
    environment_core_text = _read_if_file(repo / ENVIRONMENT_PREFLIGHT_CORE)
    # The durable planning checkpoint bootstrap intentionally wraps the established
    # environment preflight implementation. Audit the executable composition rather
    # than only the facade so moving unchanged fail-closed guards into a core module
    # cannot create a false contract failure. The guard marker itself is still
    # mandatory: removing it from both files continues to fail closed.
    environment_text = environment_wrapper_text + "\n" + environment_core_text
    issues: list[ContractIssue] = []

    def require(code: str, marker: str, detail: str, *, source: str = text) -> None:
        if marker not in source:
            issues.append(ContractIssue(code, detail))

    require("runner_image", f"runs-on: {EXPECTED_RUNNER_IMAGE}", "production runner image must be explicit, not a moving -latest alias")
    require("runner_sha_verify", 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', "exact Runner checkout must be verified before execution")
    require("engine_pin", EXPECTED_ENGINE_SHA, "canonical production Engine SHA missing")
    require("post_piper_pip_check", 'python -m pip check # post-piper-certification', "dependency graph must be rechecked after Piper installation")
    require("memory_restore_strict", "Require healthy restored cross-run memory", "production must not continue with an untrusted/empty fallback history")
    require("memory_restore_assert", 'test "${{ steps.restore_state.outputs.save_allowed }}" = "true"', "restored history must be explicitly proven save-safe")
    require("environment_preflight", "python scripts/environment_preflight.py", "runtime/media/environment capabilities must be certified before production")
    require("provider_preflight", "python scripts/provider_preflight.py", "all configured providers must be preflighted before production")
    for provider in PROVIDERS:
        require(f"provider_{provider}", f"--{provider}-key-file", f"provider preflight is missing {provider}")
    require("release_namespace_guard", "Release namespace preflight", "non-idempotent GitHub Release tag must be checked before production")
    require("release_collision_fail_closed", "existing release tag blocks this run before production", "existing release tag must fail closed rather than overwrite/retry")
    require("release_orphan_tag_guard", "/git/ref/tags/", "orphan Git tags must block before production because they bypass --target", source=environment_text)
    require("production_entrypoint", "python ../scripts/run_v3_voice.py", "canonical production entrypoint changed unexpectedly")
    require("state_persist_strict", "python scripts/state_persistence_strict.py", "accepted production state must not fail silently")
    require("release_transaction_call", "python scripts/release_transaction.py", "GitHub Release must use transactional draft/upload/verify/publish flow")
    require("release_target_binding", '--target-sha "$GITHUB_SHA"', "GitHub Release must bind to the exact reviewed Runner SHA")
    require("manual_publish", "manual_in_youtube_studio", "manual YouTube publication contract missing")
    require("partial_delivery_closed", "partial_delivery_allowed", "partial-delivery contract missing")
    require("final_master_qc", "final-master-qc.json", "Final Master QC evidence must remain in canonical closure")
    require("cleanup", "Remove plaintext production secrets and state", "plaintext secret cleanup step missing")

    if "gh release create" in text:
        issues.append(ContractIssue("direct_release_side_effect", "workflow bypasses transactional release helper"))
    if "api.pexels.com/videos/" in text:
        issues.append(ContractIssue("deprecated_pexels_endpoint", "deprecated Pexels video endpoint present"))

    # Ordering is a contract: all low-cost, non-production proofs happen before the
    # expensive/side-effectful production entrypoint.
    produce_index = text.find("python ../scripts/run_v3_voice.py")
    for code, marker in (
        ("environment_order", "python scripts/environment_preflight.py"),
        ("provider_order", "python scripts/provider_preflight.py"),
        ("memory_order", "Require healthy restored cross-run memory"),
    ):
        marker_index = text.find(marker)
        if produce_index < 0 or marker_index < 0 or marker_index > produce_index:
            issues.append(ContractIssue(code, f"{marker} must execute before production entrypoint"))

    # The state writer may use credentials, but the primary source checkout must not.
    checkout_block = text.split("- name: Checkout private engine", 1)[0]
    if "persist-credentials: true" in checkout_block:
        issues.append(ContractIssue("runner_checkout_credentials", "primary Runner checkout must not persist credentials"))

    # Production remains explicit/manual-dispatch only.
    trigger_prefix = text.split("concurrency:", 1)[0]
    if "workflow_dispatch:" not in trigger_prefix or re.search(r"(?m)^\s{2}(push|pull_request|schedule|workflow_run):", trigger_prefix):
        issues.append(ContractIssue("production_trigger", "production workflow must remain workflow_dispatch-only"))

    # Persistence is no longer allowed to hide a lost cross-run memory update.
    persist_match = re.search(
        r"- name: Persist approved encrypted cross-run memory(?P<body>.*?)(?:\n\s*- name:|\Z)",
        text,
        flags=re.S,
    )
    if persist_match and "continue-on-error: true" in persist_match.group("body"):
        issues.append(ContractIssue("state_persist_best_effort", "accepted state persistence must be a hard closure, not continue-on-error"))

    # Audit the release transaction implementation itself, not just the workflow call.
    if not release_text:
        issues.append(ContractIssue("release_transaction_missing", "transactional GitHub Release helper is missing"))
    else:
        for code, marker, detail in (
            ("release_draft_first", '"--draft"', "release must start as a draft"),
            ("release_exact_target", '"--target", target_sha', "release tag must be created from the exact reviewed Runner SHA"),
            ("release_upload", '"upload"', "release transaction must upload assets before publish"),
            ("release_remote_verify", "remote != expected", "release transaction must verify exact remote asset names, sizes and digests"),
            ("release_sha256", "hashlib.sha256()", "release assets must be verified with SHA256 rather than metadata alone"),
            ("release_digest_fail_closed", "SHA256_DIGEST_RE.fullmatch", "missing or malformed GitHub asset digests must fail closed"),
            ("release_target_verify", 'payload.get("targetCommitish")', "release target identity must be verified remotely"),
            ("release_command_timeout", "UPLOAD_TIMEOUT_SECONDS", "release commands must have bounded deadlines"),
            ("release_publish_last", '"--draft=false"', "release transaction must publish only after verification"),
            ("release_cleanup", "_delete_draft_best_effort", "failed pre-publication transaction must clean up/retain only a draft"),
        ):
            require(code, marker, detail, source=release_text)

    return issues


def assert_preproduction_contract(repo: Path) -> None:
    issues = audit_preproduction_contract(repo)
    if issues:
        joined = "; ".join(f"{item.code}: {item.detail}" for item in issues)
        raise RuntimeError("Pre-production contract failed: " + joined)


if __name__ == "__main__":
    assert_preproduction_contract(Path("."))
    print("Pre-production static contract PASS")
