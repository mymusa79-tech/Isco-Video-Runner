import pytest

from orchestration_stage_registry import (
    CachePolicy,
    DeadlinePolicy,
    ERROR_TAXONOMY,
    ImplementationBinding,
    RetryPolicy,
    StageContract,
    StageRegistry,
    StageRegistryError,
    build_l4_registry,
    certified_non_planning_contracts,
)


def _base(**overrides):
    data = dict(
        stage_id="media",
        contract_id="media-stage-v1",
        contract_version=1,
        input_schema="MediaInV1",
        input_hash_policy="semantic-canonical-v1",
        output_schema="MediaOutV1",
        semantic_rules=("trusted_bytes",),
        provider_policy={"owner": "media-core"},
        retry_policy=RetryPolicy(owner="media-core", max_attempts=2),
        cache_policy=CachePolicy(
            read=True,
            write=True,
            write_after_validation=True,
            revalidate_hits=True,
        ),
        deadline_policy=DeadlinePolicy(1_000, 5_000, "compute"),
        side_effect_policy="idempotent",
        error_taxonomy=("INVALID_STRUCTURAL", "INTERNAL_CONTRACT_ERROR"),
        evidence_schema="MediaEvidenceV1",
        implementation_binding=ImplementationBinding(
            "media-adapter", 1, "scripts/media.py", "a" * 40
        ),
    )
    data.update(overrides)
    return StageContract(**data)


def test_l4_registers_exact_non_planning_stage_set():
    registry = build_l4_registry()
    assert registry.stage_ids() == ("cinematic", "media", "qc", "render", "shorts", "tts")
    assert "planning" not in registry.stage_ids()


def test_every_contract_has_required_general_fields():
    for contract in certified_non_planning_contracts():
        assert contract.contract_id
        assert contract.contract_version >= 1
        assert contract.input_schema
        assert contract.input_hash_policy
        assert contract.output_schema
        assert contract.semantic_rules
        assert contract.provider_policy["owner"]
        assert contract.retry_policy.owner
        assert contract.cache_policy.ttl_policy
        assert contract.deadline_policy.downstream_reserve_class
        assert contract.side_effect_policy
        assert contract.error_taxonomy
        assert contract.evidence_schema
        assert contract.implementation_binding.source_sha


def test_duplicate_stage_ids_fail_loud():
    registry = StageRegistry()
    registry.register(_base())
    with pytest.raises(StageRegistryError, match="duplicate stage_id"):
        registry.register(_base(contract_id="media-stage-v2", contract_version=2))


def test_invalid_contract_version_is_rejected():
    with pytest.raises(StageRegistryError, match="contract_version"):
        _base(contract_version=0)


def test_missing_retry_owner_is_rejected():
    with pytest.raises(StageRegistryError, match="retry_policy.owner"):
        _base(retry_policy=RetryPolicy(owner="", max_attempts=1))


def test_unbounded_retry_is_rejected():
    with pytest.raises(StageRegistryError, match="bounded"):
        _base(retry_policy=RetryPolicy(owner="media", bounded=False, max_attempts=2))


def test_cache_write_before_validation_is_rejected():
    with pytest.raises(StageRegistryError, match="cache write before"):
        _base(
            cache_policy=CachePolicy(
                read=False,
                write=True,
                write_after_validation=False,
                revalidate_hits=True,
            )
        )


def test_cache_hit_without_current_contract_revalidation_is_rejected():
    with pytest.raises(StageRegistryError, match="cache hits"):
        _base(
            cache_policy=CachePolicy(
                read=True,
                write=False,
                write_after_validation=True,
                revalidate_hits=False,
            )
        )


def test_illegal_error_taxonomy_is_rejected():
    with pytest.raises(StageRegistryError, match="illegal error taxonomy"):
        _base(error_taxonomy=("SOME_TEXT_MATCHED_EXCEPTION",))


def test_all_declared_errors_are_from_typed_taxonomy():
    for contract in certified_non_planning_contracts():
        assert set(contract.error_taxonomy) <= ERROR_TAXONOMY


def test_general_error_taxonomy_matches_master_spec():
    assert ERROR_TAXONOMY == frozenset({
        "TRANSIENT_PROVIDER",
        "PROVIDER_CAPACITY",
        "INVALID_STRUCTURAL",
        "INVALID_SEMANTIC",
        "INVALID_CONTRACT_INPUT",
        "INVALID_CACHE",
        "EXHAUSTED_DEADLINE",
        "SIDE_EFFECT_RECONCILIATION_REQUIRED",
        "INTERNAL_CONTRACT_ERROR",
    })


def test_l4_does_not_register_planning_but_registry_seam_accepts_future_l5_adapter():
    registry = build_l4_registry()
    assert "planning" not in registry.stage_ids()
    registry.register_provider(
        "future-l5-adapter",
        lambda: (_base(stage_id="planning", contract_id="planning-stage-from-canonical-adapter-v1"),),
    )
    registry.load_provider("future-l5-adapter")
    assert registry.get("planning").contract_id == "planning-stage-from-canonical-adapter-v1"


def test_provider_plugin_is_atomic_on_duplicate_conflict():
    registry = StageRegistry()
    registry.register(_base())
    registry.register_provider(
        "bad-provider",
        lambda: (
            _base(stage_id="tts", contract_id="tts-stage-v1"),
            _base(stage_id="media", contract_id="media-stage-v9", contract_version=9),
        ),
    )
    with pytest.raises(StageRegistryError, match="duplicate stage_id"):
        registry.load_provider("bad-provider")
    assert registry.stage_ids() == ("media",)


def test_duplicate_provider_ids_are_rejected():
    registry = StageRegistry()
    registry.register_provider("p", lambda: ())
    with pytest.raises(StageRegistryError, match="duplicate stage provider"):
        registry.register_provider("p", lambda: ())


def test_run_snapshot_is_immutable_and_not_affected_by_later_registration():
    registry = StageRegistry()
    registry.register(_base())
    snapshot = registry.freeze_for_run()
    registry.register(_base(stage_id="tts", contract_id="tts-stage-v1"))
    assert tuple(snapshot) == ("media",)
    with pytest.raises(TypeError):
        snapshot["tts"] = registry.get("tts")


def test_implementation_binding_requires_full_blob_sha():
    with pytest.raises(StageRegistryError, match="40-char"):
        _base(implementation_binding=ImplementationBinding("x", 1, "x.py", "deadbeef"))


def test_deadline_contract_rejects_local_cap_below_minimum():
    with pytest.raises(StageRegistryError, match="local_cap_ms"):
        _base(deadline_policy=DeadlinePolicy(5_000, 4_999, "compute"))


def test_builtin_deadlines_are_explicit_policy_refs_not_invented_numbers():
    for contract in certified_non_planning_contracts():
        assert str(contract.deadline_policy.minimum_viable_ms).startswith("contract-owned:")
        assert str(contract.deadline_policy.local_cap_ms).startswith("contract-owned:")


def test_deadline_policy_rejects_untyped_free_text_reference():
    with pytest.raises(StageRegistryError, match="contract-owned reference"):
        _base(deadline_policy=DeadlinePolicy("later", "later", "compute"))


def test_builtins_preserve_existing_core_bindings_not_orchestration_executor():
    contracts = {c.stage_id: c for c in certified_non_planning_contracts()}
    assert contracts["tts"].implementation_binding.source_path == "scripts/run_v3_voice.py"
    assert contracts["media"].implementation_binding.source_path == "scripts/media_trust_boundary_v2.py"
    assert contracts["cinematic"].implementation_binding.source_path == "scripts/m7_live_binding.py"
    assert contracts["render"].implementation_binding.source_path == "scripts/render_durable_cache.py"
    assert contracts["qc"].implementation_binding.source_path == "scripts/final_master_qc.py"
    assert contracts["shorts"].implementation_binding.source_path == "scripts/shorts_production_binding.py"
    assert all("orchestration" not in c.implementation_binding.adapter_id for c in contracts.values())
