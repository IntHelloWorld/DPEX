import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mllmfl.domain.trace import build_trace, validate_trace
from mllmfl.domain.trace import project_execution
from mllmfl.domain.assertion_folding import fold_successful_assertions
from mllmfl.domain.refinement_trace import build_method_catalog, build_refinement_trace
from mllmfl.infrastructure.limited_process import run_limited
from tests.test_trace_domain import v5_events


class ParentPointerTests(unittest.TestCase):
    def test_normalized_evidence_matches_legacy_chain_representation(self):
        current = build_trace(v5_events())
        legacy = copy.deepcopy(current)
        legacy['schema_version'] = 5
        legacy['calls'][0]['parent_chain'] = [1]
        normalized = []
        for trace in (legacy, current):
            execution, folding = fold_successful_assertions(project_execution(trace, 'p.Test', 'testCase'))
            _, methods, fingerprint = build_method_catalog([execution])
            normalized.append(build_refinement_trace(
                execution, project='P', test_id='T1', test='p.Test::testCase',
                method_ids=methods, catalog_fingerprint=fingerprint,
                assertion_folding=folding, error_stack='failure', test_output='failed',
            ))
        self.assertEqual(normalized[0], normalized[1])

    def test_deep_recursion_has_linear_topology_and_preserves_values(self):
        def nested(depth):
            base = v5_events()
            result = [base[0]]
            for index in range(1, depth + 1):
                item = copy.deepcopy(base[1])
                item.update(invocation_id=index, parent_id=index - 1, seq=index + 1)
                result.append(item)
            for index in range(depth, 0, -1):
                item = copy.deepcopy(base[-2])
                item.update(invocation_id=index, seq=2 * depth - index + 2)
                result.append(item)
            result.append({**base[-1], 'seq': 2 * depth + 2})
            return build_trace(result)

        small, large = nested(1000), nested(4000)
        self.assertEqual(len(large['calls']), 3999)
        self.assertTrue(all('parent_chain' not in item for item in large['calls']))
        self.assertEqual(large['calls'][-1]['parent_invocation_id'], 3999)
        self.assertEqual(large['invocations'][-1]['arguments'], small['invocations'][-1]['arguments'])
        self.assertEqual(large['invocations'][-1]['return_value'], small['invocations'][-1]['return_value'])
        self.assertLess(len(json.dumps(large)), 4.2 * len(json.dumps(small)))

    def test_rejects_invalid_parent_topology(self):
        for mutation in ('cycle', 'missing', 'duplicate', 'chain', 'mismatch'):
            with self.subTest(mutation=mutation):
                trace = build_trace(v5_events())
                if mutation == 'cycle':
                    trace['invocations'][0]['parent_id'] = 2
                elif mutation == 'missing':
                    trace['invocations'][0]['parent_id'] = 999
                elif mutation == 'duplicate':
                    trace['calls'].append(dict(trace['calls'][0]))
                elif mutation == 'chain':
                    trace['calls'][0]['parent_chain'] = [1]
                else:
                    trace['calls'][0]['parent_invocation_id'] = 0
                with self.assertRaises(ValueError):
                    validate_trace(trace)


class LimitedProcessTests(unittest.TestCase):
    def run_child(self, code, **limits):
        with tempfile.TemporaryDirectory() as root:
            return run_limited([sys.executable, '-c', code], cwd=root,
                               env=os.environ.copy(), log_dir=Path(root),
                               timeout=limits.get('timeout', 5),
                               memory_bytes=limits.get('memory_bytes', 128 * 1024 ** 2),
                               reserve_bytes=0)

    def test_timeout_and_nonzero_exit(self):
        timed = self.run_child('import time; time.sleep(30)', timeout=0.3)
        self.assertEqual(timed['status'], 'TIMEOUT')
        self.assertNotEqual(timed['returncode'], 0)
        self.assertEqual(self.run_child('raise SystemExit(3)')['status'], 'ERROR')

    def test_group_limit_counts_child_process_memory(self):
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',\"import time; data=bytearray(48*1024**2); time.sleep(30)\"]); time.sleep(30)"
        result = self.run_child(code, memory_bytes=32 * 1024 ** 2)
        self.assertEqual(result['status'], 'MEMORY_LIMIT')
        self.assertGreater(result['peak_group_rss_swap_bytes'], 32 * 1024 ** 2)

    def test_address_space_limit_is_enforced(self):
        code = 'import resource; resource.setrlimit(resource.RLIMIT_AS,(64*1024**2,64*1024**2)); bytearray(128*1024**2)'
        self.assertEqual(self.run_child(code)['status'], 'ERROR')


if __name__ == '__main__':
    unittest.main()
