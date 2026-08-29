from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import Mock

from scripts.stage_ladder_gate import certification_tag, require_exact_sha_stage_ladder


SHA = "a" * 40
REPO = "mymusa79-tech/Isco-Video-Runner"


class _Response:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


class StageLadderGateTests(unittest.TestCase):
    def test_exact_commit_ref_passes(self) -> None:
        opener = Mock(return_value=_Response({
            "ref": f"refs/tags/{certification_tag(SHA)}",
            "object": {"type": "commit", "sha": SHA},
        }))
        self.assertEqual(
            require_exact_sha_stage_ladder(repository=REPO, sha=SHA, token="token", opener=opener),
            certification_tag(SHA),
        )
        request = opener.call_args.args[0]
        self.assertIn(certification_tag(SHA), request.full_url)

    def test_missing_ref_fails_closed(self) -> None:
        error = urllib.error.HTTPError("https://example.invalid", 404, "not found", {}, io.BytesIO())
        with self.assertRaisesRegex(RuntimeError, "no Green Stage Ladder certification"):
            require_exact_sha_stage_ladder(
                repository=REPO, sha=SHA, token="token", opener=Mock(side_effect=error)
            )

    def test_wrong_target_sha_fails_closed(self) -> None:
        opener = Mock(return_value=_Response({
            "ref": f"refs/tags/{certification_tag(SHA)}",
            "object": {"type": "commit", "sha": "b" * 40},
        }))
        with self.assertRaisesRegex(RuntimeError, "target mismatch"):
            require_exact_sha_stage_ladder(repository=REPO, sha=SHA, token="token", opener=opener)

    def test_non_commit_target_fails_closed(self) -> None:
        opener = Mock(return_value=_Response({
            "ref": f"refs/tags/{certification_tag(SHA)}",
            "object": {"type": "tag", "sha": SHA},
        }))
        with self.assertRaisesRegex(RuntimeError, "does not target a commit"):
            require_exact_sha_stage_ladder(repository=REPO, sha=SHA, token="token", opener=opener)

    def test_invalid_sha_never_queries_network(self) -> None:
        opener = Mock()
        with self.assertRaisesRegex(RuntimeError, "40-hex"):
            require_exact_sha_stage_ladder(repository=REPO, sha="main", token="token", opener=opener)
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
