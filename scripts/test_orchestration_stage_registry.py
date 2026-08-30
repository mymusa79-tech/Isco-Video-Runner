import unittest
from contextlib import contextmanager

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
    materialize_contract_data,
)


@contextmanager
def _raises(exc_type, match):
    with unittest.TestCase().assertRaisesRegex(exc_type, match):
        yield


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
    with _raises(StageRegistryError, "duplicate stage_id"):
        registry.register(_base(contract_id="media-stage-v2", contract_version=2))


def test_invalid_contract_version_is_rejected():
    with _raises(StageRegistryError, "contract_version"):
        _base(contract_version=0)


def test_missing_retry_owner_is_rejected():
    with _raises(StageRegistryError, r"retry_policy\.owner"):
        _base(retry_policy=RetryPolicy(owner="", max_attempts=1))


def test_unbounded_retry_is_rejected():
    with _raises(StageRegistryError, "bounded"):
        _base(retry_policy=RetryPolicy(owner="media", bounded=False, max_attempts=2))


def test_cache_write_before_validation_is_rejected():
    with _raises(StageRegistryError, "cache write before"):
        _base(
            cache_policy=CachePolicy(
                read=False,
                write=True,
                write_after_validation=False,
                revalidate_hits=True,
            )
        )


def test_cache_hit_without_current_contract_revalidation_is_rejected():
    with _raises(StageRegistryError, "cache hits"):
        _base(
            cache_policy=CachePolicy(
                read=True,
                write=False,
                write_after_validation=True,
                revalidate_hits=False,
            )
        )


def test_illegal_error_taxonomy_is_rejected():
    with _raises(StageRegistryError, "illegal error taxonomy"):
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
        lambda: (
            _base(
                stage_id="planning.editorial_outline",
                contract_id="planning.editorial_outline.v1",
            ),
        ),
    )
    registry.load_provider("future-l5-adapter")
    assert registry.get("planning.editorial_outline").contract_id == "planning.editorial_outline.v1"


def test_canonical_dotted_planning_stage_ids_are_valid_without_rewriting_identity():
    contract = _base(
        stage_id="planning.append_only_repair",
        contract_id="planning.append_only_repair.exact.v1",
    )
    assert contract.stage_id == "planning.append_only_repair"


def test_malformed_dotted_stage_ids_are_rejected():
    for stage_id in (".planning", "planning.", "planning..outline", "Planning.outline", "planning.-outline"):
        with _raises(StageRegistryError, "invalid stage_id"):
            _base(stage_id=stage_id)


def test_structured_schema_and_semantic_rules_are_frozen_without_field_loss():
    output_schema = {
        "type": "object",
        "properties": {"sections": {"type": "array", "items": {"type": "string"}}},
        "required": ["sections"],
    }
    semantic_rules = {
        "kind": "script",
        "expected_ids": ["s1", "s2"],
        "exact_order": True,
    }
    contract = _base(output_schema=output_schema, semantic_rules=semantic_rules)
    assert materialize_contract_data(contract.output_schema) == output_schema
    assert materialize_contract_data(contract.semantic_rules) == semantic_rules
    with _raises(TypeError, ""):
        contract.output_schema["type"] = "array"


def test_cache_policy_can_retain_canonical_policy_as_lossless_adapter_metadata():
    canonical = {
        "read": True,
        "write": True,
        "revalidate_on_hit": True,
        "evict_invalid": True,
        "namespace": "planning-stage-contract-v1",
    }
    policy = CachePolicy(
        read=True,
        write=True,
        write_after_validation=True,
        revalidate_hits=True,
        canonical_policy=canonical,
    )
    contract = _base(cache_policy=policy)
    assert materialize_contract_data(contract.cache_policy.canonical_policy) == canonical
    with _raises(TypeError, ""):
        contract.cache_policy.canonical_policy["namespace"] = "changed"


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
    with _raises(StageRegistryError, "duplicate stage_id"):
        registry.load_provider("bad-provider")
    assert registry.stage_ids() == ("media",)


def test_duplicate_provider_ids_are_rejected():
    registry = StageRegistry()
    registry.register_provider("p", lambda: ())
    with _raises(StageRegistryError, "duplicate stage provider"):
        registry.register_provider("p", lambda: ())


def test_request_specific_resolver_does_not_mutate_static_registry():
    registry = build_l4_registry()
    before = registry.stage_ids()
    registry.register_resolver(
        "planning-canonical-v1",
        lambda expected_ids: _base(
            stage_id="planning.full_script",
            contract_id="planning.full_script.v1",
            output_schema={"type": "object", "required": ["sections"]},
            semantic_rules={
                "kind": "script",
                "expected_ids": list(expected_ids),
                "exact_order": True,
            },
        ),
    )
    first = registry.resolve("planning-canonical-v1", ("s1", "s2"))
    second = registry.resolve("planning-canonical-v1", ("s3",))
    assert materialize_contract_data(first.semantic_rules)["expected_ids"] == ["s1", "s2"]
    assert materialize_contract_data(second.semantic_rules)["expected_ids"] == ["s3"]
    assert registry.stage_ids() == before


def test_duplicate_resolver_ids_are_rejected():
    registry = StageRegistry()
    registry.register_resolver("planning", lambda: _base())
    with _raises(StageRegistryError, "duplicate stage resolver"):
        registry.register_resolver("planning", lambda: _base())


def test_resolver_must_return_stage_contract():
    registry = StageRegistry()
    registry.register_resolver("bad", lambda: {"stage_id": "planning.full_script"})
    with _raises(StageRegistryError, "did not return StageContract"):
        registry.resolve("bad")


def test_run_snapshot_is_immutable_and_not_affected_by_later_registration():
    registry = StageRegistry()
    registry.register(_base())
    snapshot = registry.freeze_for_run()
    registry.register(_base(stage_id="tts", contract_id="tts-stage-v1"))
    assert tuple(snapshot) == ("media",)
    with _raises(TypeError, ""):
        snapshot["tts"] = registry.get("tts")


def test_implementation_binding_requires_full_blob_sha():
    with _raises(StageRegistryError, "40-char"):
        _base(implementation_binding=ImplementationBinding("x", 1, "x.py", "deadbeef"))


def test_deadline_contract_rejects_local_cap_below_minimum():
    with _raises(StageRegistryError, "local_cap_ms"):
        _base(deadline_policy=DeadlinePolicy(5_000, 4_999, "compute"))


def test_builtin_deadlines_are_explicit_policy_refs_not_invented_numbers():
    for contract in certified_non_planning_contracts():
        assert str(contract.deadline_policy.minimum_viable_ms).startswith("contract-owned:")
        assert str(contract.deadline_policy.local_cap_ms).startswith("contract-owned:")


def test_deadline_policy_rejects_untyped_free_text_reference():
    with _raises(StageRegistryError, "contract-owned reference"):
        _base(deadline_policy=DeadlinePolicy("later", "later", "compute"))


def test_builtins_use_certified_stable_ports_for_all_l7_stages():
    contracts = {c.stage_id: c for c in certified_non_planning_contracts()}
    expected = {
        "tts": ("tts-runtime-port-v1", "scripts/orchestration_tts_port.py"),
        "media": ("media-runtime-port-v1", "scripts/orchestration_media_port.py"),
        "cinematic": ("cinematic-runtime-port-v1", "scripts/orchestration_cinematic_port.py"),
        "render": ("render-runtime-port-v1", "scripts/orchestration_render_port.py"),
        "qc": ("qc-runtime-port-v1", "scripts/orchestration_qc_port.py"),
        "shorts": ("shorts-runtime-port-v1", "scripts/orchestration_shorts_port.py"),
    }
    for stage_id, (adapter_id, source_path) in expected.items():
        assert contracts[stage_id].implementation_binding.adapter_id == adapter_id
        assert contracts[stage_id].implementation_binding.source_path == source_path


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            suite.addTest(unittest.FunctionTestCase(func, description=name))
    return suite


if __name__ == "__main__":
    unittest.main()
