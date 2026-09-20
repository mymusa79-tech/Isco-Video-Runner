from __future__ import annotations

import hashlib
import json
from pathlib import Path

from clean_v2 import mistral_executor, providers
from clean_v2.contracts import ContractError, compute_brief_sha256, validate_script
from clean_v2.pipeline import _script_prompt


EXPECTED_BRIEF_SHA256 = "bcf8d3017ee8e18ee4808614c7c07731b0452a5ba6182e1183404f663078e129"

# Exact plan and narrative transitions captured from Run #155, attempt 2,
# immediately before the real Script-stage Mistral contract rejection.
PLAN = {
    "promise": "سنتعرف معًا على أسباب فشل خططنا اليومية وكيفية تحويل نوايانا إلى إجراءات عملية",
    "schema_version": 1,
    "sections": [
        {
            "heading": "مقدمة: لماذا نخطط ثم نفشل؟",
            "id": "s1",
            "purpose": "توضيح المشكلة العامة وتحديد الأهداف للعرض",
            "visual_query_en": "hand writing schedule on notebook close-up of calendar pages no faces",
        },
        {
            "heading": "الخطأ في التخطيط: التوقعات غير الواقعية",
            "id": "s2",
            "purpose": "شرح مفهوم الخطأ في التخطيط وكيف يضعف الجهود",
            "visual_query_en": "hand sketching timeline on whiteboard slow motion of clock ticking",
        },
        {
            "heading": "الانحراف: لماذا نؤجل؟",
            "id": "s3",
            "purpose": "توضيح أن التأجيل هو نتيجة للانحراف الذاتي وليس الكسل",
            "visual_query_en": "hand scrolling phone laptop open with unfinished task dim office lighting",
        },
        {
            "heading": "الحل: التخطيط التنفيذي (if‑then)",
            "id": "s4",
            "purpose": "عرض تقنية التخطيط التنفيذي وكيف تُحسن المتابعة",
            "visual_query_en": "hand writing if‑then plan on sticky notes placing them on a desk",
        },
        {
            "heading": "خطوة واحدة: تطبيق الفكرة في يومك",
            "id": "s5",
            "purpose": "توجيه المشاهد لتطبيق خطة if‑then في لحظة محددة من يومه",
            "visual_query_en": "hand placing a sticky note on a coffee mug person sipping coffee no faces",
        },
    ],
    "title": "لماذا تفشل خطط إدارة الوقت في الحياة اليومية",
}

TRANSITIONS = [
    "فلنغوص في السبب الحقيقي",
    "والآن نكشف عن العامل الثالث",
    "دعونا نجرب الفكرة في حياتنا اليومية",
]


def _safe_shape(value: object, raw: str) -> dict[str, object]:
    result: dict[str, object] = {
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "raw_utf8_bytes": len(raw.encode("utf-8")),
        "json_type": type(value).__name__,
    }
    if not isinstance(value, dict):
        return result
    result["top_level_keys"] = sorted(str(key) for key in value.keys())
    sections = value.get("sections")
    result["sections_type"] = type(sections).__name__
    if isinstance(sections, list):
        result["sections_count"] = len(sections)
        result["sections"] = [
            {
                "index": index,
                "json_type": type(item).__name__,
                "keys": sorted(str(key) for key in item.keys()) if isinstance(item, dict) else [],
                "id": str(item.get("id") or "")[:40] if isinstance(item, dict) else "",
                "narration_type": type(item.get("narration")).__name__ if isinstance(item, dict) else None,
                "narration_chars": (
                    len(item.get("narration"))
                    if isinstance(item, dict) and isinstance(item.get("narration"), str)
                    else None
                ),
            }
            for index, item in enumerate(sections[:10])
        ]
    return result


def main() -> None:
    brief_path = Path("engine/production/approved_brief.json")
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    actual_brief_sha = compute_brief_sha256(brief)
    if actual_brief_sha != EXPECTED_BRIEF_SHA256:
        raise SystemExit(
            f"brief_sha_mismatch expected={EXPECTED_BRIEF_SHA256} actual={actual_brief_sha}"
        )

    prompt = _script_prompt(brief, PLAN, transitions=TRANSITIONS)
    value = providers._mistral_call(prompt, 7500, "script")
    raw = mistral_executor.get_last_mistral_executor_raw_content()

    try:
        validate_script(value, PLAN)
    except ContractError as exc:
        diagnostic = providers._safe_mistral_script_raw_diagnostic(raw, exc)
        print(
            "RUN155_MISTRAL_SCRIPT_SCHEMA_PROBE="
            + json.dumps(
                {
                    "status": "contract_rejected",
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "diagnostic": diagnostic,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return

    print(
        "RUN155_MISTRAL_SCRIPT_SCHEMA_PROBE="
        + json.dumps(
            {
                "status": "contract_pass",
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "shape": _safe_shape(value, raw),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
