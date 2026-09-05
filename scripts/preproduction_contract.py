from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


PRODUCTION_WORKFLOW = Path(".github/workflows/produce-resilient-v4.yml")
RELEASE_TRANSACTION = Path("scripts/release_transaction.py")
ENVIRONMENT_PREFLIGHT = Path("scripts/environment_preflight.py")
ENVIRONMENT_PREFLIGHT_CORE = Path("scripts/environment_preflight_core.py")
EXPECTED_RUNNER_IMAGE = "ubuntu-24.04"
PROVIDERS = ("gemini", "groq", "openrouter", "pexels", "pixabay")
PROVIDER_PREFLIGHT_COMMAND = "python -m scripts.provider_preflight"


@dataclass(frozen=True)
class ContractIssue:
    code: str
    detail: str


def _read_if_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _canonical_engine_pin_issues(text: str) -> list[ContractIssue]:
    """Validate one Engine identity without duplicating a mutable SHA in Python.

    The production workflow is the canonical V4 Engine authority. A previous static
    EXPECTED_ENGINE_SHA constant in this module made every legitimate Engine upgrade
    require a second hand-edited SHA and caused the Stage Ladder to reject the newer
    production pin. Audit the three live workflow bindings instead: admission
    EXPECTED_ENGINE_SHA, private Engine checkout ref, and runtime ISCO_ENGINE_SHA.
    """
    expected = re.findall(
        r"^\s*EXPECTED_ENGINE_SHA:\s*([0-9a-f]{40})\s*$",
        text,
        flags=re.MULTILINE,
    )
    checkout = re.findall(
        r"^\s+ref:\s+([0-9a-f]{40})\s*$",
        text,
        flags=re.MULTILINE,
    )
    runtime = re.findall(
        r"^\s+ISCO_ENGINE_SHA:\s*([0-9a-f]{40})\s*$",
        text,
        flags=re.MULTILINE,
    )
    issues: list[ContractIssue] = []
    if len(expected) != 1 or len(checkout) != 1 or len(runtime) != 1:
        issues.append(
            ContractIssue(
                "engine_pin",
                "canonical production Engine SHA must appear exactly once in admission, checkout, and runtime bindings",
            )
        )
        return issues
    if not (expected[0] == checkout[0] == runtime[0]):
        issues.append(
            ContractIssue(
                "engine_pin_mismatch",
                "canonical production Engine admission, checkout, and runtime SHAs must be identical",
            )
        )
    return issues


def audit_preproduction_contract(repo: Path) -> list[ContractIssue]:
    text = (repo / PRODUCTION_WORKFLOW).read_text(encoding="utf-8")
    release_text = _read_if_file(repo / RELEASE_TRANSACTION)
    environment_wrapper_text = _read_if_file(repo / ENVIRONMENT_PREFLIGHT)
    environment_core_text = _read_if_file(repo / ENVIRONMENT_PREFLIGHT_CORE)
    # The environment facade may delegate to a core module. Audit the executable
    # composition rather than requiring safety markers to remain as YAML comments.
    environment_text = environment_wrapper_text + "\n" + environment_core_text
    issues: list[ContractIssue] = []

    def require(code: str, marker: str, detail: str, *, source: str = text) -> None:
        if marker not in source:
            issues.append(ContractIssue(code, detail))

    require("runner_image", f"runs-on: {EXPECTED_RUNNER_IMAGE}", "production runner image must be explicit, not a moving -latest alias")
    require("runner_sha_verify", 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', "exact Runner checkout must be verified before execution")
    issues.extend(_canonical_engine_pin_issues(text))
    require("post_piper_pip_check", 'python -m pip check # post-piper-certification', "dependency graph must be rechecked after Piper installation")
    require("memory_restore_strict", "Require healthy restored cross-run memory", "production must not continue with an untrusted/empty fallback history")
    require("memory_restore_assert", 'test "${{ steps.restore_state.outputs.save_allowed }}" = "true"', "restored history must be explicitly proven save-safe")
    require("environment_preflight", "python scripts/environment_preflight.py", "runtime/media/environment capabilities must be certified before production")
    require(
        "provider_preflight",
        PROVIDER_PREFLIGHT_COMMAND,
        "all configured providers must be preflighted before production",
    )
    for provider in PROVIDERS:
        require(f"provider_{provider}", f"--{provider}-key-file", f"provider preflight is missing {provider}")

    # Release namespace ownership moved into environment_preflight_core so manual and
    # Telegram ingress use the same pre-provider guard and the exact resolved tag.
    # Same-SHA reconciliation is allowed only as an idempotent recovery path; different
    # targets, orphan tags, unverifiable published receipts, or media drift remain hard
    # failures and are never overwritten.
    require(
        "release_namespace_guard",
        "_release_namespace_status(",
        "GitHub Release namespace must be checked before production",
        source=environment_text,
    )
    require(
        "release_collision_target_guard",
        "existing release tag belongs to a different Runner SHA",
        "existing release on a different Runner SHA must fail closed",
        source=environment_text,
    )
    require("release_orphan_tag_guard", "/git/ref/tags/", "orphan Git tags must block before production because they bypass --target", source=environment_text)

    release_reconciliation_markers = (
        '_assert_release_identity(existing, tag=tag, target_sha=target_sha)',
        '_download_and_verify_receipt(',
        '_assert_current_media_matches_receipt(assets, receipt_assets)',
        '_remove_exact_existing_draft(',
    )
    if not release_text or any(marker not in release_text for marker in release_reconciliation_markers):
        issues.append(
            ContractIssue(
                "release_collision_fail_closed",
                "same-SHA Release reconciliation must verify identity, durable receipt/media bytes, and strictly replace only an exact draft",
            )
        )

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
        ("provider_order", PROVIDER_PREFLIGHT_COMMAND),
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
