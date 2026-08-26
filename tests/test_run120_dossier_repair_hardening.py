from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import run120_dossier_repair_hardening as hardening


def _section(section_id: str, words: int = 120):
    narration = " ".join([f"كلمة{section_id}"] * words)
    return SimpleNamespace(
        id=section_id,
        narration=narration,
        key_point=f"فكرة {section_id}",
        visual_query=f"visual {section_id}",
        on_screen_text=f"text {section_id}",
        emotion="calm",
        expected_seconds=60.0,
    )


def _plan(count: int = 8):
    return SimpleNamespace(
        topic="موضوع الاختبار",
        format="film",
        sections=[_section(f"S{i}") for i in range(1, count + 1)],
        hook="hook",
        title_options=["a", "b", "c"],
        thumbnail_concepts=["x", "y", "z"],
        cta="cta",
        closing_payoff="payoff",
        identity_opener="OPENER",
        identity_closer="CLOSER",
        identity_transitions=["t1", "t2", "t3"],
        narrative_format="direct_cinematic",
        editorial_intent={"editorial_thesis": "thesis"},
    )


def _stub_engine_invariants(monkeypatch):
    monkeypatch.setattr(hardening.staged, "load_editorial_policy", lambda: {"brand_signature": {}})
    monkeypatch.setattr(hardening.staged, "_writer_policy_json", lambda value: "{}")
    monkeypatch.setattr(hardening.staged, "_compact_planning_policy_json", lambda value: value)
    monkeypatch.setattr(hardening.staged, "_compact_planning_research_json", lambda value: value)
    monkeypatch.setattr(hardening.staged, "_strip_host_managed_phrases", lambda *args, **kwargs: None)
    monkeypatch.setattr(hardening.staged, "_apply_brand_signature", lambda *args, **kwargs: None)
    monkeypatch.setattr(hardening.staged, "_assert_brand_signature_invariant", lambda *args, **kwargs: None)
    monkeypatch.setattr(hardening.staged, "_reject_unverified_religious_quotes", lambda *args, **kwargs: None)
    monkeypatch.setattr(hardening.staged, "_strip_exact_host_phrase", lambda text, phrase: text)


def _success_for(ids):
    return {
        section_id: {"narration": f"مصَحح {section_id} " * 120, "key_point": f"مصَحح {section_id}"}
        for section_id in ids
    }


def test_targeted_dossier_repair_touches_only_target_sections(monkeypatch):
    _stub_engine_invariants(monkeypatch)
    plan = _plan()
    before = {section.id: section.narration for section in plan.sections}
    calls = []

    def fake_call(_key, _prompt, _model, expected_ids):
        calls.append(list(expected_ids))
        return _success_for(expected_ids)

    monkeypatch.setattr(hardening, "_one_schema_bounded_call", fake_call)
    issue_notes = "- [editorial_review] structural_anti_ai:duplicate_sentence\nTARGET_SECTION_IDS=[\"S3\",\"S4\"]"
    repaired = hardening._repair_existing_plan(
        plan,
        issue_notes,
        api_key="k",
        topic=plan.topic,
        requested_format="film",
        content_model="model",
        research_context={},
    )

    assert calls == [["S3", "S4"]]
    assert repaired.sections[2].narration != before["S3"]
    assert repaired.sections[3].narration != before["S4"]
    for section in repaired.sections:
        if section.id not in {"S3", "S4"}:
            assert section.narration == before[section.id]
    # Input plan is a checkpoint: it is never mutated before a complete repair returns.
    assert {section.id: section.narration for section in plan.sections} == before


def test_global_dossier_repair_uses_two_section_shards(monkeypatch):
    _stub_engine_invariants(monkeypatch)
    plan = _plan()
    calls = []

    def fake_call(_key, _prompt, _model, expected_ids):
        calls.append(list(expected_ids))
        return _success_for(expected_ids)

    monkeypatch.setattr(hardening, "_one_schema_bounded_call", fake_call)
    hardening._repair_existing_plan(
        plan,
        "- [tone] naturalness_flag",
        api_key="k",
        topic=plan.topic,
        requested_format="film",
        content_model="model",
        research_context={},
    )
    assert calls == [["S1", "S2"], ["S3", "S4"], ["S5", "S6"], ["S7", "S8"]]


def test_transport_pressure_splits_only_failed_shard_and_does_not_replay_success(monkeypatch):
    _stub_engine_invariants(monkeypatch)
    plan = _plan(4)
    calls = []

    def fake_call(_key, _prompt, _model, expected_ids):
        ids = list(expected_ids)
        calls.append(ids)
        if ids == ["S3", "S4"]:
            raise hardening._DossierTransportPressure("finish_reason=length")
        return _success_for(ids)

    monkeypatch.setattr(hardening, "_one_schema_bounded_call", fake_call)
    hardening._repair_existing_plan(
        plan,
        "- [tone] naturalness_flag",
        api_key="k",
        topic=plan.topic,
        requested_format="film",
        content_model="model",
        research_context={},
    )
    assert calls == [["S1", "S2"], ["S3", "S4"], ["S3"], ["S4"]]
    assert calls.count(["S1", "S2"]) == 1


def test_single_section_transport_pressure_fails_closed(monkeypatch):
    _stub_engine_invariants(monkeypatch)
    plan = _plan(2)

    def fake_call(_key, _prompt, _model, expected_ids):
        ids = list(expected_ids)
        if len(ids) == 2:
            raise hardening._DossierTransportPressure("finish_reason=length")
        if ids == ["S1"]:
            raise hardening._DossierTransportPressure("finish_reason=length")
        return _success_for(ids)

    monkeypatch.setattr(hardening, "_one_schema_bounded_call", fake_call)
    with pytest.raises(hardening._DossierTransportPressure):
        hardening._repair_existing_plan(
            plan,
            "- [tone] naturalness_flag",
            api_key="k",
            topic=plan.topic,
            requested_format="film",
            content_model="model",
            research_context={},
        )


def test_normal_call_schema_mismatch_gets_exactly_one_schema_repair(monkeypatch):
    calls = []

    def fake_json(_key, prompt, model=None):
        calls.append(prompt)
        if len(calls) == 1:
            return {"sections": []}
        return {"sections": [{"id": "S1", "narration": "نص", "key_point": "فكرة"}]}

    monkeypatch.setattr(hardening.staged, "json_text", fake_json)
    result = hardening._one_schema_bounded_call("k", "prompt", "m", ["S1"])
    assert result["S1"]["narration"] == "نص"
    assert len(calls) == 2


def test_provider_truncation_does_not_replay_same_prompt_as_schema_repair(monkeypatch):
    calls = []

    def fake_json(_key, prompt, model=None):
        calls.append(prompt)
        raise RuntimeError(
            "All free providers failed for planning subtask: "
            "groq:GROQ_PREMATURE_RESPONSE finish_reason=length | "
            "openrouter:OPENROUTER_PREMATURE_RESPONSE finish_reason=length"
        )

    monkeypatch.setattr(hardening.staged, "json_text", fake_json)
    with pytest.raises(hardening._DossierTransportPressure):
        hardening._one_schema_bounded_call("k", "prompt", "m", ["S1", "S2"])
    assert len(calls) == 1


def test_fatal_auth_or_budget_failure_never_becomes_shard_split():
    assert not hardening._is_transport_pressure(
        RuntimeError("AI budget authorization denied; finish_reason=length")
    )
    assert not hardening._is_transport_pressure(
        RuntimeError("unauthorized; finish_reason=length")
    )


def test_capacity_and_length_failures_are_transport_pressure():
    assert hardening._is_transport_pressure(RuntimeError("GROQ_PREMATURE_RESPONSE finish_reason=length"))
    assert hardening._is_transport_pressure(RuntimeError("GROQ_TPM_CAPACITY_PREFLIGHT estimated_total=9000"))


def test_issue_compaction_keeps_verdict_and_drops_duplicate_plan_payload():
    notes = (
        "- [tone] naturalness_flag\n"
        "[LOCAL_STRUCTURAL_REPAIR_SCOPE]\nTARGET_SECTION_IDS=[\"S2\"]\nTARGET_SECTIONS=huge"
    )
    compact = hardening._compact_issue_notes(notes)
    assert compact == "- [tone] naturalness_flag"
    assert "TARGET_SECTIONS" not in compact
