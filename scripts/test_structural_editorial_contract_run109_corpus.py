from __future__ import annotations

import unittest
from dataclasses import dataclass

from scripts.structural_editorial_contract import _current_flags


@dataclass
class Section:
    narration: str


class Run109StructuralCorpusTests(unittest.TestCase):
    def test_observed_run109_family_is_detected_by_engine_owner(self) -> None:
        sections = [
            Section("ليست في كسلك أو عجزك الدائم عن الالتزام، بل في كون الجداول لا تراعي يومك."),
            Section("ليس تسويفًا بالمعنى الكسول، بل هو رد فعل طبيعي على عبء غير واضح."),
            Section("ليس جداول أكثر صرامة أو تطبيقات معقدة، بل الانتقال إلى قرار صغير يمكن تكراره."),
            Section("ليست ضبط الساعات بالثانية ولا مراقبة العقارب بقسوة، بل إدارة الانتباه. ليس الوقت عدواً تقاومه، بل مساحة تعيش فيها."),
        ]
        self.assertIn("repeated_not_x_but_y", _current_flags(sections))


if __name__ == "__main__":
    unittest.main()
