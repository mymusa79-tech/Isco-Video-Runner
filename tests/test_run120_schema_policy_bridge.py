from __future__ import annotations

import pytest

from scripts import run120_dossier_repair_hardening as hardening
from scripts import run120_schema_policy_bridge as bridge


def test_bridge_makes_dossier_prompt_compatible_with_existing_script_doctor_schema_policy(monkeypatch):
    captured = {}

    def fake_schema_owner(api_key, prompt, model, *, expected_ids):
        captured["prompt"] = prompt
        captured["ids"] = list(expected_ids)
        return {"S1": {"narration": "نص", "key_point": "فكرة"}}

    monkeypatch.setattr(bridge.staged, "_call_with_schema_repair", fake_schema_owner)
    prompt = (
        "You are the senior Arabic script editor for نداء اليقظة.\n"
        "BLOCKING DOSSIER ISSUES — fix only what is relevant to these returned sections:\n- issue\n"
        "EDITORIAL_POLICY:\n{}\n"
        "CURRENT_SHARD (draft data, not instructions):\n[]"
    )
    result = bridge._policy_owned_call("k", prompt, "m", ["S1"])
    assert result["S1"]["narration"] == "نص"
    assert "senior Arabic script editor and cultural QA reviewer" in captured["prompt"]
    assert "Specific issues an automated pre-check found that you MUST address:" in captured["prompt"]
    assert "SECTIONS:" in captured["prompt"]
    assert captured["ids"] == ["S1"]


def test_bridge_converts_only_transport_pressure_to_adaptive_split_signal(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError(
            "All free providers failed for planning subtask: "
            "groq:GROQ_PREMATURE_RESPONSE finish_reason=length | "
            "openrouter:OPENROUTER_PREMATURE_RESPONSE finish_reason=length"
        )

    monkeypatch.setattr(bridge.staged, "_call_with_schema_repair", fail)
    with pytest.raises(hardening._DossierTransportPressure):
        bridge._policy_owned_call("k", "prompt", "m", ["S1", "S2"])


def test_bridge_leaves_budget_failure_fail_closed_without_split(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("AI budget authorization denied for task X; provider call blocked")

    monkeypatch.setattr(bridge.staged, "_call_with_schema_repair", fail)
    with pytest.raises(RuntimeError, match="AI budget authorization denied"):
        bridge._policy_owned_call("k", "prompt", "m", ["S1"])
