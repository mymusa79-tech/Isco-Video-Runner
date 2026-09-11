from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_deferred_sibling_shorts_v1 import validate_deferred_parent


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DeferredSiblingWorkerTests(unittest.TestCase):
    def _fixture(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix='deferred-sibling-worker-'))
        (root / 'plan.json').write_text(json.dumps({'format': 'film'}), encoding='utf-8')
        parent = {
            'schema_version': 1,
            'contract_id': 'post_gold.sibling_parent_contract.v1',
            'approved_by_user': True,
            'kind': 'long',
            'approval_scope': 'long_plus_sibling_shorts',
            'production_dispatch_authorized': False,
            'request_id': 'req-parent',
            'request_sha256': 'approved-sha',
            'approved_topic': 'topic',
            'candidate': {'hook_potential': 0.9},
        }
        parent_path = root / 'sibling-short-parent-contract.json'
        parent_path.write_text(json.dumps(parent, sort_keys=True), encoding='utf-8')
        sibling_plan = {
            'schema_version': 1,
            'source_request_id': 'req-parent',
            'source_request_sha256': 'approved-sha',
            'source_production_plan_sha256': _sha(root / 'plan.json'),
            'source_topic': 'topic',
            'short_count': 2,
            'semantic_jobs': [
                {'index': 1, 'semantic_job': 'one', 'status': 'planned_not_dispatched', 'production_dispatch_authorized': False},
                {'index': 2, 'semantic_job': 'two', 'status': 'planned_not_dispatched', 'production_dispatch_authorized': False},
            ],
            'automatic_production_started': False,
        }
        plan_path = root / 'sibling-short-plan.json'
        plan_path.write_text(json.dumps(sibling_plan, sort_keys=True), encoding='utf-8')
        master = {
            'schema_version': 1,
            'contract_id': 'post_gold.parent_master_lock.v1',
            'state': 'locked_after_gold',
            'mutation_allowed': False,
            'parent_media_rebuild_allowed': False,
            'release_candidate_tag': 'video-parent',
            'final': {'file': 'final.mp4', 'size': 1234, 'sha256': 'f' * 64},
        }
        master_path = root / 'master-lock.json'
        master_path.write_text(json.dumps(master, sort_keys=True), encoding='utf-8')
        deferred = {
            'schema_version': 1,
            'contract_id': 'post_gold.sibling_short_deferred.v1',
            'status': 'deferred_after_parent_gold',
            'parent_request_id': 'req-parent',
            'parent_request_sha256': 'approved-sha',
            'parent_final_sha256': 'f' * 64,
            'master_lock': {'file': 'master-lock.json', 'sha256': _sha(master_path)},
            'parent_contract': {'file': 'sibling-short-parent-contract.json', 'sha256': _sha(parent_path)},
            'sibling_short_plan': {'file': 'sibling-short-plan.json', 'sha256': _sha(plan_path), 'short_count': 2},
            'execution_owner': 'isolated_child_jobs',
            'automatic_production_started': False,
            'provider_calls_performed': False,
            'blocking_parent_delivery': False,
            'partial_child_delivery_allowed': False,
        }
        (root / 'sibling-short-deferred.json').write_text(json.dumps(deferred, sort_keys=True), encoding='utf-8')
        return root

    def test_sealed_parent_contract_is_accepted(self) -> None:
        root = self._fixture()
        parent, plan_path, master = validate_deferred_parent(root, parent_release_tag='video-parent')
        self.assertEqual(parent['request_id'], 'req-parent')
        self.assertEqual(plan_path.name, 'sibling-short-plan.json')
        self.assertEqual(master['final']['sha256'], 'f' * 64)

    def test_changed_sibling_plan_fails_closed(self) -> None:
        root = self._fixture()
        path = root / 'sibling-short-plan.json'
        plan = json.loads(path.read_text(encoding='utf-8'))
        plan['short_count'] = 3
        path.write_text(json.dumps(plan), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'changed after parent delivery'):
            validate_deferred_parent(root, parent_release_tag='video-parent')

    def test_wrong_parent_release_tag_fails_closed(self) -> None:
        root = self._fixture()
        with self.assertRaisesRegex(RuntimeError, 'release tag differs'):
            validate_deferred_parent(root, parent_release_tag='other-parent')

    def test_parent_contract_mutation_fails_closed(self) -> None:
        root = self._fixture()
        path = root / 'sibling-short-parent-contract.json'
        parent = json.loads(path.read_text(encoding='utf-8'))
        parent['approved_topic'] = 'changed'
        path.write_text(json.dumps(parent), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'changed after parent delivery'):
            validate_deferred_parent(root, parent_release_tag='video-parent')


if __name__ == '__main__':
    unittest.main()
