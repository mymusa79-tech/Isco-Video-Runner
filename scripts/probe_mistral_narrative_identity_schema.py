from __future__ import annotations

import hashlib
import json
from pathlib import Path

from clean_v2 import providers
from clean_v2.contracts import compute_brief_sha256, validate_narrative_identity
from clean_v2.pipeline import _narrative_identity_prompt

EXPECTED_BRIEF_SHA256 = "bcf8d3017ee8e18ee4808614c7c07731b0452a5ba6182e1183404f663078e129"

PLAN = {
    "promise": "سنتعرف معًا على أسباب فشل خططنا اليومية وكيفية تحويل نوايانا إلى إجراءات عملية",
    "schema_version": 1,
    "sections": [
        {"heading": "مقدمة: لماذا نخطط ثم نفشل؟", "id": "s1", "purpose": "توضيح المشكلة العامة وتحديد الأهداف للعرض", "visual_query_en": "hand writing schedule on notebook close-up of calendar pages no faces"},
        {"heading": "الخطأ في التخطيط: التوقعات غير الواقعية", "id": "s2", "purpose": "شرح مفهوم الخطأ في التخطيط وكيف يضعف الجهود", "visual_query_en": "hand sketching timeline on whiteboard slow motion of clock ticking"},
        {"heading": "الانحراف: لماذا نؤجل؟", "id": "s3", "purpose": "توضيح أن التأجيل هو نتيجة للانحراف الذاتي وليس الكسل", "visual_query_en": "hand scrolling phone laptop open with unfinished task dim office lighting"},
        {"heading": "الحل: التخطيط التنفيذي (if-then)", "id": "s4", "purpose": "عرض تقنية التخطيط التنفيذي وكيف تُحسن المتابعة", "visual_query_en": "hand writing if-then plan on sticky notes placing them on a desk"},
        {"heading": "خطوة واحدة: تطبيق الفكرة في يومك", "id": "s5", "purpose": "توجيه المشاهد لتطبيق خطة if-then في لحظة محددة من يومه", "visual_query_en": "hand placing a sticky note on a coffee mug person sipping coffee no faces"},
    ],
    "title": "لماذا تفشل خطط إدارة الوقت في الحياة اليومية",
}


def main() -> None:
    brief_path = Path("engine/production/approved_brief.json")
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    actual = compute_brief_sha256(brief)
    if actual != EXPECTED_BRIEF_SHA256:
        raise SystemExit(f"brief_sha_mismatch expected={EXPECTED_BRIEF_SHA256} actual={actual}")

    from isco_video_agent.config import load_editorial_policy

    signature = load_editorial_policy().get("brand_signature") or {}
    canonical_opener = str(signature.get("opener") or "").strip()
    canonical_closer = str(signature.get("closer") or "").strip()
    if not canonical_opener or not canonical_closer:
        raise SystemExit("missing_brand_signature")

    prompt = _narrative_identity_prompt(
        brief=brief,
        plan=PLAN,
        canonical_opener=canonical_opener,
        canonical_closer=canonical_closer,
    )
    value = providers._mistral_call(prompt, 900, "narrative_identity")
    normalized = validate_narrative_identity(value)

    print(
        "MISTRAL_NARRATIVE_IDENTITY_SCHEMA_PROBE="
        + json.dumps(
            {
                "status": "contract_pass",
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "top_level_keys": sorted(value.keys()),
                "opener_chars": len(normalized["opener"]),
                "closer_chars": len(normalized["closer"]),
                "transitions_count": len(normalized["transitions"]),
                "transition_chars": [len(item) for item in normalized["transitions"]],
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
