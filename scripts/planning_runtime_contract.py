from __future__ import annotations

"""Canonical Runner-owned planning runtime seam.

Every Runner patch that can change planning prompts, provider routing, planner output,
repair semantics, immutable approved-input binding, or plan-level fallback must be
activated through this module. Durable planning-state binding hashes the transitive
import closure rooted here, so planning changes invalidate incompatible checkpoints
while unrelated production changes do not.

Do not install planning-affecting patches directly from run_v3_voice.py or
runtime_closure.py. Add them here in the correct lifecycle phase instead; doing so both
activates them and automatically enters them into the planning contract hash.
"""

from scripts.append_retry_guard import install_append_retry_guard
from scripts.attempt10_append_bound_recovery import install_attempt10_append_bound_recovery
from scripts.attempt9_schema_normalizer import install_attempt9_schema_normalizer
from scripts.bounded_output_recovery import install_bounded_output_recovery
from scripts.brand_anchor_guard import install_brand_anchor_guard
from scripts.dynamic_planning_capacity import install_dynamic_planning_capacity
from scripts.gemini_planning_output_guard import install_gemini_planning_output_guard
from scripts.immutable_planning_snapshot import install_runtime_snapshot_binding
from scripts.planner_quality_guard import install_planner_quality_guard
from scripts.planner_schema_guard import install_schema_guard
from scripts.planning_batch_hardening import install_planning_batch_hardening
from scripts.planning_capacity_headroom import install_planning_capacity_headroom
from scripts.planning_capacity_profile import install_planning_capacity_profile
from scripts.planning_legacy_authority_guard import install_legacy_planning_authority_guard
from scripts.planning_stage_contract import (
    assert_planning_stage_contract_installed,
    install_planning_contract_router,
    install_planning_stage_boundaries,
)
from scripts.producer_quality_contract import install_planning_producer_quality_contract
from scripts.product_proof_plan import install_product_proof_fallback
from scripts.provider_capacity_hardening import install_provider_capacity_hardening
from scripts.run120_dossier_repair_hardening import install_run120_dossier_repair_hardening
from scripts.run120_schema_policy_bridge import install_run120_schema_policy_bridge
from scripts.run123_budget_closure import install_run123_budget_closure
from scripts.run124_terminal_provider_recovery import install_run124_terminal_provider_recovery
from scripts.run125_cache_prefix_contract import install_run125_cache_prefix_contract
from scripts.run125_capacity_routing_closure import install_run125_capacity_routing_closure
from scripts.runtime_patch_contracts import certify_runtime_patch_contracts
from scripts.runtime_phase import canonical_runtime_enabled
from scripts.schema_repair_policy import install_schema_repair_policy
from scripts.short_planning_repair import install_short_planning_repair
from scripts.short_repair_reset_recovery import install_short_repair_reset_recovery
from scripts.task_level_planner_router import install_router


# runtime_closure is intentionally unit-testable in isolation. Such a test must not
# fabricate an entrypoint contract that production would normally install earlier.
# Once the canonical entrypoint has bootstrapped the explicit Stage Contract, however,
# every later lifecycle phase is fail-closed: any patch that loses the router/boundaries
# is an INTERNAL_CONTRACT_ERROR rather than a silent fallback to historical behavior.
_ENTRYPOINT_STAGE_CONTRACT_BOOTSTRAPPED = False


def _reassert_after_lifecycle_patch() -> None:
    # Reassert both halves of the contract. json_text may still contain the routed
    # wrapper while a later installer has replaced only the compatibility schema seam.
    install_planning_contract_router()
    install_planning_stage_boundaries()
    if _ENTRYPOINT_STAGE_CONTRACT_BOOTSTRAPPED:
        assert_planning_stage_contract_installed()


def install_entrypoint_planning_contracts() -> None:
    """Install the planning stack that precedes runtime_closure in canonical V4."""
    global _ENTRYPOINT_STAGE_CONTRACT_BOOTSTRAPPED

    # Keep the provider/router composition order identical to the historical seam.
    # The explicit Stage Contract then replaces the legacy prompt-inferred json_text
    # owner before any Planning call can occur.
    install_run123_budget_closure()
    install_schema_guard()
    install_provider_capacity_hardening()
    install_router()
    install_planning_contract_router()
    install_planning_batch_hardening()
    install_schema_repair_policy()
    install_run120_dossier_repair_hardening()
    # Moment uses Engine's native one-section schema instead of the long-form resilient
    # planner. Install its in-place Dossier transport immediately after the long-form
    # repair owner so both formats share the same explicit repair lifecycle without
    # prompt inference or full-plan regeneration.
    install_short_planning_repair()
    install_run120_schema_policy_bridge()
    install_planner_quality_guard()
    install_attempt9_schema_normalizer()
    install_append_retry_guard()
    # These wrappers are deliberately installed after batch/repair/append owners so
    # stage identity is attached to the final live call boundaries, never prompt text.
    install_planning_stage_boundaries()
    assert_planning_stage_contract_installed()
    _ENTRYPOINT_STAGE_CONTRACT_BOOTSTRAPPED = True


def install_runtime_planning_contracts() -> None:
    """Install the planning/recovery portion historically owned by runtime_closure."""
    # Workflow bootstrap materializes the immutable brief snapshot in an earlier
    # process. Rebind those verified bytes inside the live production process before
    # any runtime planning patch can build durable checkpoint identity.
    if canonical_runtime_enabled():
        install_runtime_snapshot_binding()

    install_attempt10_append_bound_recovery()
    install_bounded_output_recovery()
    install_schema_repair_policy()
    install_gemini_planning_output_guard()
    install_run124_terminal_provider_recovery()
    install_run125_capacity_routing_closure()
    install_dynamic_planning_capacity()
    install_run125_cache_prefix_contract()
    # Final planning-capacity layer is intentionally after the historical Run125/128
    # ownership stack. It adds operational headroom, a format-native Moment envelope,
    # all-path OpenRouter preflight enforcement, and a bounded native-Short terminal
    # reset owner without replacing the existing long-form shard recovery semantics.
    install_planning_capacity_profile()
    install_planning_capacity_headroom()
    # Runs #158/#160 reached the compact Moment RepairDossier after Draft/Review, but
    # that transport sat outside the native-Short reset owner. Reuse the exact same
    # evidence-backed <=60s wait + one retry for the surgical repair call only; Dossier
    # max_attempts and all semantic/quality gates remain unchanged.
    install_short_repair_reset_recovery()

    # Certify the historical routing/capacity composition first. No provider call is
    # made by certification. Then rebind explicit stage wrappers around any function
    # a runtime installer replaced. In an isolated runtime_closure unit test there is
    # deliberately no entrypoint bootstrap to assert. In canonical production there
    # is, so loss of the explicit router remains fail-closed.
    certify_runtime_patch_contracts()
    _reassert_after_lifecycle_patch()


def install_post_runtime_planning_contracts() -> None:
    """Install final plan-level guards, then the producer contract as the last pre-audit owner."""
    install_brand_anchor_guard()
    install_product_proof_fallback()
    # Plan-level wrappers may replace build/repair surfaces. Reassert the explicit
    # Planning contract at the final canonical seam. The final seal then removes the
    # dormant prompt-hash checkpoint loader/writer from runtime authority entirely.
    _reassert_after_lifecycle_patch()
    if _ENTRYPOINT_STAGE_CONTRACT_BOOTSTRAPPED:
        install_legacy_planning_authority_guard()
    # This is intentionally last: it adds the same producer directive to initial
    # writing and every RepairDossier regeneration, then validates the returned plan
    # before independent factuality/content/tone audits spend provider budget.
    install_planning_producer_quality_contract()
