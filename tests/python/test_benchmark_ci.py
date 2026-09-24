# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import importlib.util
import json
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'benchmark_ci', Path(__file__).resolve().parents[2] / '.github/scripts/benchmark_ci.py'
)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)
HEAD = 'a' * 40
BASE = 'b' * 40


def result(output='', code=0):
    return CompletedProcess(['git'], code, stdout=output, stderr='')


class RevisionSelectionTest(unittest.TestCase):
    def test_pr_uses_base_and_head(self):
        with patch.object(ci, 'git', side_effect=[result(HEAD), result()]) as git:
            selected = ci.select_revisions('pull_request', {'pull_request': {'base': {'sha': BASE}, 'head': {'sha': HEAD}}})
        self.assertEqual(selected['baseline'], BASE)
        self.assertEqual(selected['candidate'], HEAD)
        self.assertEqual(selected['skip_reason'], '')
        self.assertEqual(git.call_args.args, ('cat-file', '-e', BASE + '^{commit}'))

    def test_push_uses_before_commit(self):
        with patch.object(ci, 'git', side_effect=[result(HEAD), result()]):
            selected = ci.select_revisions('push', {'before': BASE, 'after': HEAD})
        self.assertEqual(selected['baseline'], BASE)

    def test_new_branch_has_no_baseline(self):
        with patch.object(ci, 'git', return_value=result(HEAD)):
            selected = ci.select_revisions('push', {'before': '0' * 40, 'after': HEAD})
        self.assertEqual(selected['baseline'], '')
        self.assertIn('candidate only', selected['skip_reason'])

    def test_manual_run_uses_parent(self):
        with patch.object(ci, 'git', side_effect=[result(HEAD), result(BASE), result()]):
            selected = ci.select_revisions('workflow_dispatch', {})
        self.assertEqual(selected['baseline'], BASE)

    def test_manual_root_commit_skips_comparison(self):
        with patch.object(ci, 'git', side_effect=[result(HEAD), result(code=1)]):
            selected = ci.select_revisions('workflow_dispatch', {})
        self.assertEqual(selected['baseline'], '')

    def test_fetches_missing_baseline(self):
        with patch.object(ci, 'git', side_effect=[result(HEAD), result(code=1), result(), result()]) as git:
            selected = ci.select_revisions('push', {'before': BASE, 'after': HEAD})
        self.assertEqual(selected['baseline'], BASE)
        self.assertEqual(git.call_args_list[2].args, ('fetch', '--no-tags', '--depth=1', 'origin', BASE))

    def test_unfetchable_baseline_keeps_candidate(self):
        with patch.object(ci, 'git', side_effect=[result(HEAD), result(code=1), result(code=1)]):
            selected = ci.select_revisions('push', {'before': BASE, 'after': HEAD})
        self.assertEqual(selected['baseline'], '')
        self.assertIn(BASE, selected['skip_reason'])

    def test_fetch_timeout_keeps_candidate(self):
        with patch.object(ci, 'git', side_effect=[result(HEAD), result(code=1), TimeoutExpired('git fetch', 60)]):
            selected = ci.select_revisions('push', {'before': BASE, 'after': HEAD})
        self.assertEqual(selected['baseline'], '')
        self.assertIn('could not be fetched', selected['skip_reason'])

    def test_rejects_wrong_candidate_and_invalid_baseline(self):
        for event in ({'before': BASE, 'after': BASE}, {'before': '--bad-ref', 'after': HEAD}):
            with self.subTest(event=event), patch.object(ci, 'git', return_value=result(HEAD)):
                with self.assertRaises(RuntimeError):
                    ci.select_revisions('push', event)


class SummaryTest(unittest.TestCase):
    def test_regression_summary_and_annotation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'revisions.json').write_text(json.dumps({'candidate': HEAD, 'baseline': BASE, 'skip_reason': ''}))
            comparison = root / 'comparison-fixture'
            comparison.mkdir()
            (comparison / 'comparison.json').write_text(json.dumps({
                'status': 'warning', 'threshold_percent': 20, 'repeats': 3, 'settings': {'clients': 50},
                'comparisons': [{'command': 'GET', 'metric': 'rps', 'baseline_median': 100,
                                 'candidate_median': 70, 'change_percent': -30, 'regression': True}],
            }))
            with patch('builtins.print') as output:
                summary = ci.render_summary(root, 'success', 'success')
            self.assertIn(HEAD, summary)
            self.assertIn(BASE, summary)
            self.assertIn('-30.00%', summary)
            self.assertIn('WARNING', summary)
            output.assert_called_once()
            self.assertTrue(output.call_args.args[0].startswith('::warning::'))

    def test_candidate_only_summary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'revisions.json').write_text(json.dumps({
                'candidate': HEAD, 'baseline': '', 'skip_reason': 'No baseline; measuring the candidate only.'
            }))
            run = root / 'run-fixture'
            run.mkdir()
            (run / 'results.json').write_text(json.dumps({
                'status': 'success', 'settings': {},
                'results': [{'command': 'SET', 'rps': 100, 'p50_latency_ms': 0.1}],
            }))
            summary = ci.render_summary(root, 'success', 'success')
            self.assertIn('No regression verdict', summary)
            self.assertIn('candidate only', summary)
            self.assertIn('| SET | 100.00 | 0.100 |', summary)

    def test_build_failure_without_measurements(self):
        with TemporaryDirectory() as directory:
            summary = ci.render_summary(Path(directory), 'skipped', 'failure')
        self.assertIn('**failure**', summary)
        self.assertIn('No measurements were produced', summary)

    def test_failed_comparison_is_not_reported_as_success(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            comparison = root / 'comparison-fixture'
            comparison.mkdir()
            (comparison / 'comparison.json').write_text(json.dumps({
                'status': 'failed', 'threshold_percent': 20, 'repeats': 3, 'settings': {},
                'comparisons': [], 'error': 'Kvrocks exited during benchmarking',
            }))
            with patch('builtins.print') as output:
                summary = ci.render_summary(root, 'failure', 'failure')
            self.assertIn('Comparison: **failed**', summary)
            self.assertIn('Comparison failed or was interrupted', summary)
            output.assert_not_called()

    def test_prepare_persists_revisions_and_action_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            event = root / 'event.json'
            event.write_text(json.dumps({'before': BASE, 'after': HEAD}))
            output = root / 'output'
            with patch.dict(ci.os.environ, {'GITHUB_EVENT_NAME': 'push', 'GITHUB_EVENT_PATH': str(event),
                                           'GITHUB_OUTPUT': str(output)}), \
                    patch.object(ci, 'git', side_effect=[result(HEAD), result()]):
                ci.prepare(root / 'artifacts')
            self.assertEqual(output.read_text(), f'baseline={BASE}\n')
            self.assertEqual(json.loads((root / 'artifacts/revisions.json').read_text())['candidate'], HEAD)


if __name__ == '__main__':
    unittest.main()
