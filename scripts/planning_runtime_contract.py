from __future__ import annotations

"""Canonical Runner-owned planning runtime seam.

Every Runner patch that can change planning prompts, provider routing, planner output,
repair semantics, or plan-level fallback must be activated through this module. Durable
planning-state binding hashes the transitive import closure rooted here, so planning
changes invalidate incompatible checkpoints while unrelated production changes do not.

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
from scripts.planner_quality_guard import install_planner_quality_guard
from scripts.planner_schema_guard import install_schema_guard
from scripts.planning_batch_hardening import install_planning_batch_hardening
from scripts.product_proof_plan import install_product_proof_fallback
from scripts.provider_capacity_hardening import install_provider_capacity_hardening
from scripts.run120_dossier_repair_hardening import install_run120_dossier_repair_hardening
from scripts.run120_schema_policy_bridge import install_run120_schema_policy_bridge
from scripts.run123_budget_closure import install_run123_budget_closure
from scripts.run124_terminal_provider_recovery import install_run124_terminal_provider_recovery
from scripts.run125_cache_prefix_contract import install_run125_cache_prefix_contract
from scripts.run125_capacity_routing_closure import install_run125_capacity_routing_closure
from scripts.runtime_patch_contracts import certify_runtime_patch_contracts
from scripts.schema_repair_policy import install_schema_repair_policy
from scripts.task_level_planner_router import install_router


def install_entrypoint_planning_contracts() -> None:
    """Install the planning stack that precedes runtime_closure in canonical V4."""
    # Keep this order identical to the historical run_v3_voice composition. Several
    # wrappers intentionally nest around earlier owners, so ordering is contract data.
    install_run123_budget_closure()
    install_schema_guard()
    install_provider_capacity_hardening()
    install_router()
    install_planning_batch_hardening()
    install_schema_repair_policy()
    install_run120_dossier_repair_hardening()
    install_run120_schema_policy_bridge()
    install_planner_quality_guard()
    install_attempt9_schema_normalizer()
    install_append_retry_guard()


def install_runtime_planning_contracts() -> None:
    """Install the planning/recovery portion historically owned by runtime_closure."""
    # This runs after immutable snapshot activation and before media/release wrappers,
    # preserving the exact canonical runtime order while giving planning its own seam.
    install_attempt10_append_bound_recovery()
    install_bounded_output_recovery()
    install_schema_repair_policy()
    install_gemini_planning_output_guard()
    install_run124_terminal_provider_recovery()
    install_run125_capacity_routing_closure()
    install_dynamic_planning_capacity()
    install_run125_cache_prefix_contract()

    # Certify the final composed planning monkey-patch surface before provider/media
    # work. This stays in the planning seam because it checks the live routing/capacity
    # composition that determines whether a checkpoint can be safely continued.
    certify_runtime_patch_contracts()


def install_post_runtime_planning_contracts() -> None:
    """Install plan-level guards/fallbacks that historically follow runtime_closure."""
    install_brand_anchor_guard()
    install_product_proof_fallback()
