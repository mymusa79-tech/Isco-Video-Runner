from clean_v2 import text_audit
from clean_v2.pipeline import _factuality_repair_issue_notes, _factuality_target_section_ids


def test_dynamic_schema_uses_actual_section_ids():
    schema = text_audit._text_audit_schema(("s1", "s2", "s3"))
    item = schema["properties"]["unsupported_claims"]["items"]
    assert item["properties"]["section_id"]["enum"] == ["s1", "s2", "s3"]
    assert item["required"] == ["section_id", "issue"]
    assert item["additionalProperties"] is False


def test_structured_section_id_is_authoritative_across_issue_wording():
    script = {"sections": [
        {"id": "s1", "narration": "ألف"},
        {"id": "s2", "narration": "باء"},
        {"id": "s3", "narration": "جيم"},
    ]}
    wordings = [
        "Section two may overstate causation.",
        "هذه ملاحظة بصياغة عربية لا تذكر رقم القسم إطلاقًا.",
        "Claim wording differs completely between providers.",
        "لا يوجد هنا s2 ولا Section 2 ولا اقتباس من النص.",
    ]
    for issue in wordings:
        report = {
            "diagnostics": {
                "raw_result": {
                    "unsupported_claims": [{"section_id": "s2", "issue": issue}],
                    "professional_advice_flags": [],
                    "expert_persona_flags": [],
                }
            }
        }
        assert _factuality_target_section_ids(report, script) == ("s2",)
        assert _factuality_repair_issue_notes(report) == f"- [factuality:s2] {issue}"


def test_validator_rejects_missing_or_unknown_section_id():
    good = {
        "status": "block",
        "unsupported_claims": [{"section_id": "s2", "issue": "claim"}],
        "professional_advice_flags": [],
        "expert_persona_flags": [],
        "notes": [],
    }
    assert text_audit._validate_factuality_result(good, ("s1", "s2", "s3")) is good

    bad_missing = {**good, "unsupported_claims": [{"issue": "claim"}]}
    try:
        text_audit._validate_factuality_result(bad_missing, ("s1", "s2", "s3"))
    except ValueError:
        pass
    else:
        raise AssertionError("missing section_id accepted")

    bad_unknown = {**good, "unsupported_claims": [{"section_id": "s9", "issue": "claim"}]}
    try:
        text_audit._validate_factuality_result(bad_unknown, ("s1", "s2", "s3"))
    except ValueError:
        pass
    else:
        raise AssertionError("unknown section_id accepted")
