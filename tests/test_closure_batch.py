import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    'closure_batch', Path(__file__).resolve().parents[1] / 'scripts/run_closure_refinement.py'
)
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)


class ClosureBatchTests(unittest.TestCase):
    def test_workers_default_comes_from_batch_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'batch': {'workers': 30}}))
            self.assertEqual(batch.configured_workers(config), 30)
            for value in (0, True, '30'):
                config.write_text(json.dumps({'batch': {'workers': value}}))
                with self.assertRaises(ValueError):
                    batch.configured_workers(config)

    def test_rejects_corrupt_or_mismatched_worker_result(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = batch.RunLayout(Path(directory))
            args = SimpleNamespace(timeout=5, collect_timeout=10,
                                   collect_memory_gib=1, host_reserve_gib=1)
            for payload in ('{bad', '{}', json.dumps({
                'schema': 'isolated-collection-result', 'schema_version': 1,
                'rows': [{'project': 'Closure', 'bug': '99', 'status': 'OK'}],
            })):
                def fake_worker(command, **kwargs):
                    Path(command[command.index('--result') + 1]).write_text(payload)
                    return {'status': 'OK'}
                with patch.object(batch, 'run_limited', fake_worker):
                    with self.assertRaises(ValueError):
                        batch.collect_isolated(layout, '4', Path(directory) / 'config.json', args)

    def fixture(self, root, names):
        (root / 'predictions').mkdir(exist_ok=True)
        (root / 'sample_manifest.json').write_text(json.dumps({
            'schema': 'autofl_sample_manifest', 'schema_version': 1, 'bugs': names,
        }))
        for name in names:
            (root / 'predictions' / f'XFL-{name}.json').write_text(json.dumps({
                'messages': [{'role': 'assistant', 'content': 'p.Type.method()'}],
            }))

    def test_manifest_validation_and_numeric_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, ['Closure_10', 'Closure_2'])
            self.assertEqual(batch.load_inputs(root), ['2', '10'])
            with self.assertRaises(ValueError):
                batch.load_inputs(root, {'99'})
            for names in ([], ['Closure_2', 'Closure_2'], ['Chart_1']):
                self.fixture(root, names)
                with self.assertRaises(ValueError):
                    batch.load_inputs(root)
            (root / 'sample_manifest.json').write_text('{bad')
            with self.assertRaises(json.JSONDecodeError):
                batch.load_inputs(root)

    def test_dry_run_gates_api_and_failures_remain_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root, ['Closure_1', 'Closure_2', 'Closure_3'])
            config = root / 'config.json'
            config.write_text(json.dumps({'dpex': {
                'vision_model': 'deepseek-flash', 'api_key_env': 'BATCH_TEST_KEY',
            }, 'uml': {
                'max_upstream_calls': 4,
                'max_downstream_calls': 4,
                'max_internal_calls': 6,
            }}))
            calls = []
            viewport_calls = []

            def fake_collect(*args, **kwargs):
                bug = args[1]
                return [{'project': 'Closure', 'bug': bug, 'status': 'OK'}]

            def fake_refine(*args):
                bug, dry_run = args[2], args[7]
                calls.append((bug, dry_run))
                viewport_calls.append(args[9:12])
                if bug == '3':
                    raise TimeoutError('simulated request timeout')
                return {'status': 'ERROR' if bug == '2' else ('DRY_RUN' if dry_run else 'OK')}

            argv = ['batch', '--root', str(root / 'run'), '--locator-results', str(root),
                    '--config', str(config), '--workers', '2']
            with patch.object(batch.sys, 'argv', argv), \
                    patch.dict(batch.os.environ, {'BATCH_TEST_KEY': 'test'}), \
                    patch.object(batch, 'collect_isolated', fake_collect), \
                    patch.object(batch, '_refine_bug', fake_refine):
                self.assertEqual(batch.main(), 1)
            self.assertIn(('1', False), calls)
            self.assertNotIn(('2', False), calls)
            self.assertNotIn(('3', False), calls)
            state = json.loads((root / 'run/summaries/batch_status.json').read_text())
            self.assertEqual(state['counts'], {'OK': 1, 'ERROR': 2})
            self.assertEqual(state['phase'], 'COMPLETED_WITH_ERRORS')
            self.assertEqual(state['viewport'], {
                'max_upstream_calls': 4,
                'max_downstream_calls': 4,
                'max_internal_calls': 6,
            })
            self.assertTrue(viewport_calls)
            self.assertEqual(set(viewport_calls), {(4, 4, 6)})


if __name__ == '__main__':
    unittest.main()
