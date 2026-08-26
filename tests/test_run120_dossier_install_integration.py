from __future__ import annotations

from types import SimpleNamespace

from scripts import run120_dossier_repair_hardening as hardening


def test_installer_keeps_original_apply_and_reaudit_but_bypasses_full_build_during_repair(monkeypatch):
    calls = {"full_build": 0, "repair_existing": 0, "reaudit": 0, "supplied_repair": 0}
    plan = SimpleNamespace(topic="topic", format="film")
    repaired_plan = SimpleNamespace(topic="topic", format="film", marker="repaired")

    def original_full_build(*args, **kwargs):
        calls["full_build"] += 1
        return SimpleNamespace(marker="unexpected-full-rebuild")

    def original_apply(dossier, current_plan, *, repair_fn, reaudit_fn, max_attempts=1):
        candidate = repair_fn(current_plan, "- blocking issue")
        calls["reaudit"] += 1
        reaudit_fn(candidate)
        return candidate

    def repair_existing(current_plan, issue_notes, **kwargs):
        calls["repair_existing"] += 1
        assert current_plan is plan
        assert "blocking issue" in issue_notes
        return repaired_plan

    monkeypatch.delattr(
        hardening.orchestrator,
        "_ISCO_RUN120_DOSSIER_REPAIR_HARDENED",
        raising=False,
    )
    monkeypatch.setattr(hardening.staged, "build_plan", original_full_build)
    monkeypatch.setattr(hardening.orchestrator, "apply_single_repair", original_apply)
    monkeypatch.setattr(hardening, "_repair_existing_plan", repair_existing)

    hardening.install_run120_dossier_repair_hardening()

    # Outside a dossier repair context, normal initial planning still delegates to the
    # exact captured build_plan implementation.
    normal = hardening.staged.build_plan(
        "k", "topic", "film", "model", research_context={}, avoid_context={}
    )
    assert normal.marker == "unexpected-full-rebuild"
    assert calls["full_build"] == 1

    def supplied_engine_repair_fn(current_plan, issue_notes):
        calls["supplied_repair"] += 1
        # This mirrors orchestrator._repair_fn calling the routed build_plan while the
        # P1 budget scope is active. The installed context makes this one call in-place.
        return hardening.staged.build_plan(
            "k",
            current_plan.topic,
            current_plan.format,
            "model",
            research_context={},
            avoid_context={},
            revision_note="Independent quality review found issues to address: " + issue_notes,
            allow_fallback=False,
        )

    result = hardening.orchestrator.apply_single_repair(
        object(),
        plan,
        repair_fn=supplied_engine_repair_fn,
        reaudit_fn=lambda candidate: {"candidate": candidate},
        max_attempts=2,
    )

    assert result is repaired_plan
    assert calls["supplied_repair"] == 1
    assert calls["repair_existing"] == 1
    assert calls["reaudit"] == 1
    # Most important regression: dossier repair does NOT enter original full build.
    assert calls["full_build"] == 1
