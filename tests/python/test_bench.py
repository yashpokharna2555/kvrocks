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
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location('kvrocks_x', Path(__file__).resolve().parents[2] / 'x.py')
x = importlib.util.module_from_spec(spec)
spec.loader.exec_module(x)

CSV = ('"test","rps","avg_latency_ms","min_latency_ms","p50_latency_ms",'
       '"p95_latency_ms","p99_latency_ms","max_latency_ms"\n'
       '"SET","10000.00","0.200","0.010","0.100","0.400","0.500","0.600"\n'
       '"GET","20000.00","0.100","0.010","0.050","0.200","0.300","0.400"\n')


class BenchTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output_dir = temporary.name
        self.csv = CSV
        self.diagnostics = ''
        self.server = MagicMock()
        self.server.poll.return_value = None
        self.benchmark = MagicMock()
        self.benchmark.poll.return_value = 0
        self.benchmark.returncode = 0
        self.popen = self.patch('Popen', side_effect=self.start_process)
        self.patch('subprocess_run', return_value=MagicMock(stdout='redis-benchmark 7.0.15\n'))
        self.patch('find_command', return_value='/usr/bin/redis-benchmark')
        self.patch('Path.is_file', return_value=True)
        self.patch('time.sleep')
        self.clock = self.patch('time.monotonic', return_value=0)
        connection = self.patch('socket.create_connection')
        self.client = connection.return_value.__enter__.return_value
        self.client.recv.side_effect = [b'+PO', b'NG\r\n']

    def start_process(self, args, **kwargs):
        if 'cwd' in kwargs:
            kwargs['stdout'].write('Server diagnostic output\n')
            return self.server
        kwargs['stdout'].write(self.csv)
        kwargs['stderr'].write(self.diagnostics)
        return self.benchmark

    def run_bench(self, **kwargs):
        x.bench(bench_path='redis-benchmark', output_dir=self.output_dir, **{'dir': 'build', **kwargs})

    def report(self):
        return json.loads(next(Path(self.output_dir).glob('run-*/results.json')).read_text())

    def patch(self, name, **kwargs):
        patcher = patch.object(x, name, **kwargs)
        if '.' in name:
            owner, attribute = name.split('.')
            patcher = patch.object(getattr(x, owner), attribute, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_success_cleans_up_server_and_workspace(self):
        self.run_bench()
        self.client.sendall.assert_called_once_with(b'*1\r\n$4\r\nPING\r\n')
        self.server.terminate.assert_called_once()
        self.server.wait.assert_called_once_with(timeout=10)
        workspace = self.popen.call_args_list[0].kwargs['cwd']
        self.assertFalse(Path(workspace).exists())
        report = self.report()
        self.assertEqual(report['status'], 'success')
        self.assertEqual(report['results'][0]['rps'], 10000)
        self.assertEqual(report['results'][1]['p50_latency_ms'], 0.05)
        self.assertEqual(next(Path(self.output_dir).glob('run-*/benchmark.csv')).read_text(), CSV)

    def test_missing_binary_does_not_start_server(self):
        with patch.object(x.Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'binary not found'):
                self.run_bench(dir='missing')
        self.popen.assert_not_called()

    def test_invalid_settings_are_rejected(self):
        for value in (0, -1, 2147483648):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'positive 32-bit'):
                self.run_bench(requests=value)
        self.popen.assert_not_called()

    def test_startup_failure_does_not_start_benchmark(self):
        self.server.poll.return_value = 1
        self.server.returncode = 1
        with self.assertRaisesRegex(RuntimeError, 'exited during startup'):
            self.run_bench()
        self.assertEqual(self.popen.call_count, 1)

    def test_startup_timeout_stops_server(self):
        self.clock.side_effect = [0, 31]
        with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
            self.run_bench()
        self.server.terminate.assert_called_once()

    def test_benchmark_failure_stops_server(self):
        self.benchmark.returncode = 1
        with self.assertRaisesRegex(RuntimeError, 'redis-benchmark failed'):
            self.run_bench()
        self.server.terminate.assert_called_once()
        self.assertEqual(self.report()['status'], 'failed')
        self.assertEqual(self.report()['results'], [])
        self.assertIn('Server diagnostic', next(Path(self.output_dir).glob('run-*/server.log')).read_text())

    def test_benchmark_timeout_stops_both_processes(self):
        self.benchmark.poll.return_value = None
        self.clock.side_effect = [0, 0, 0, 121]
        with self.assertRaisesRegex(RuntimeError, '120-second timeout'):
            self.run_bench()
        self.benchmark.terminate.assert_called_once()
        self.server.terminate.assert_called_once()

    def test_server_crash_stops_benchmark(self):
        self.server.poll.side_effect = [None, None, 1, 1]
        self.server.returncode = 1
        self.benchmark.poll.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'Kvrocks exited during benchmarking'):
            self.run_bench()
        self.benchmark.terminate.assert_called_once()

    def test_interruption_stops_both_processes(self):
        self.benchmark.poll.side_effect = [KeyboardInterrupt, None]
        with self.assertRaises(KeyboardInterrupt):
            self.run_bench()
        self.benchmark.terminate.assert_called_once()
        self.server.terminate.assert_called_once()
        self.assertEqual(self.report()['status'], 'interrupted')

    def test_error_output_fails_even_with_zero_exit(self):
        self.diagnostics = 'Error from server: ERR unsupported command\n'
        with self.assertRaisesRegex(RuntimeError, 'reported errors'):
            self.run_bench()
        self.assertEqual(self.report()['status'], 'failed')

    def test_warning_output_is_preserved(self):
        self.diagnostics = 'WARNING: benchmark warning\n'
        self.run_bench()
        self.assertEqual(self.report()['status'], 'success')
        self.assertEqual(next(Path(self.output_dir).glob('run-*/benchmark.stderr.log')).read_text(), self.diagnostics)

    def test_invalid_csv_is_saved_without_successful_measurements(self):
        self.csv = 'Error from server: ERR unsupported command\n'
        with self.assertRaisesRegex(RuntimeError, 'Invalid benchmark output'):
            self.run_bench()
        self.assertEqual(self.report()['results'], [])
        self.assertEqual(next(Path(self.output_dir).glob('run-*/benchmark.csv')).read_text(), self.csv)

    def test_workload_options_are_forwarded_and_recorded(self):
        self.run_bench(requests=20000, clients=20, data_size=64, pipeline=4)
        report = self.report()
        for flag, value in [('-n', '20000'), ('-c', '20'), ('-d', '64'), ('-P', '4')]:
            command = report['benchmark_command']
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(report['settings']['data_size'], 64)

    def test_repeated_runs_keep_separate_artifacts(self):
        self.run_bench()
        self.client.recv.side_effect = [b'+PONG\r\n']
        self.run_bench()
        self.assertEqual(len(list(Path(self.output_dir).glob('run-*/results.json'))), 2)

    def test_shutdown_timeout_kills_server(self):
        self.server.wait.side_effect = [x.TimeoutExpired('kvrocks', 10), 0]
        self.run_bench()
        self.server.kill.assert_called_once()


class BenchmarkParserTest(unittest.TestCase):
    def test_rejects_incomplete_duplicate_and_invalid_measurements(self):
        cases = [
            '', '\n'.join(CSV.splitlines()[:2]), CSV + CSV.splitlines()[1] + '\n',
            CSV.replace('10000.00', 'nan'), CSV.replace('10000.00', 'inf'),
            CSV.replace('10000.00', '-1'), CSV.replace('10000.00', '0'),
            CSV.replace('0.200', '-1'), CSV.replace('10000.00', 'bad'),
            CSV.replace('"10000.00",', ''), CSV + 'Error from server: ERR failed\n',
            '"SET","10000"\n"GET","20000"\n',
        ]
        for output in cases:
            with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, 'Invalid benchmark output'):
                x.parse_benchmark_results(output)

    def test_cli_accepts_options_after_build_directory(self):
        result = subprocess.run(
            [sys.executable, '-B', str(Path(x.__file__)), 'bench', 'build', '--requests', '0'],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('positive 32-bit', result.stderr)


class BenchmarkComparisonTest(unittest.TestCase):
    def run_result(self, rps=10000, latency=1):
        return {'status': 'success', 'settings': {'requests': 10000}, 'benchmark_version': 'fixture',
                'results': [{'command': command, 'rps': rps, 'avg_latency_ms': latency,
                             'p50_latency_ms': latency, 'p95_latency_ms': latency, 'p99_latency_ms': latency}
                            for command in ('SET', 'GET')]}

    def test_median_ignores_single_outlier(self):
        baseline = [self.run_result(rps=value) for value in (10000, 10000, 1000000)]
        candidate = [self.run_result(rps=value) for value in (10000, 10000, 1)]
        comparisons = x.compare_benchmark_results(baseline, candidate, 20)
        self.assertFalse(any(item['regression'] for item in comparisons))
        self.assertEqual(comparisons[0]['candidate_median'], 10000)

    def test_throughput_threshold_boundary_and_improvement(self):
        for rps, expected in ((8001, False), (8000, True), (7999, True), (12000, False)):
            with self.subTest(rps=rps):
                comparison = x.compare_benchmark_results([self.run_result()], [self.run_result(rps=rps)], 20)
                self.assertEqual(comparison[0]['regression'], expected)
                self.assertAlmostEqual(comparison[0]['change_percent'], (rps - 10000) / 100)

    def test_latency_increase_is_regression_but_decrease_is_not(self):
        for latency, expected in ((1.25, True), (1.2, True), (1.199, False), (0.75, False)):
            with self.subTest(latency=latency):
                comparison = x.compare_benchmark_results([self.run_result()], [self.run_result(latency=latency)], 20)
                self.assertEqual(comparison[1]['regression'], expected)

    def test_zero_latency_baseline_has_no_percentage(self):
        comparison = x.compare_benchmark_results([self.run_result(latency=0)], [self.run_result()], 20)
        self.assertIsNone(comparison[1]['change_percent'])
        self.assertFalse(comparison[1]['regression'])
        json.dumps(comparison, allow_nan=False)

    def test_rejects_failed_or_incompatible_runs(self):
        for field, value in [('status', 'failed'), ('settings', {}), ('benchmark_version', 'other')]:
            candidate = self.run_result()
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                x.compare_benchmark_results([self.run_result()], [candidate], 20)
        with self.assertRaises(RuntimeError):
            x.compare_benchmark_results([], [], 20)
        with self.assertRaises(RuntimeError):
            x.compare_benchmark_results([self.run_result()], [], 20)

    def test_warning_keeps_successful_exit_and_alternates_builds(self):
        with TemporaryDirectory() as workspace:
            def measure(dir, bench_path, **kwargs):
                return self.run_result(rps=7000 if Path(dir).name == 'candidate' else 10000)
            with patch.object(x, 'bench', side_effect=measure) as runner:
                x.bench_compare('baseline', 'candidate', output_dir=workspace)
            order = [Path(call.args[0]).name for call in runner.call_args_list]
            self.assertEqual(order, ['baseline', 'candidate', 'candidate', 'baseline', 'baseline', 'candidate'])
            report = json.loads(next(Path(workspace).glob('comparison-*/comparison.json')).read_text())
            self.assertEqual(report['status'], 'warning')
            self.assertEqual(len(report['baseline_runs']), 3)
            self.assertEqual(len(report['candidate_runs']), 3)
            self.assertAlmostEqual(report['comparisons'][0]['change_percent'], -30)
            self.assertEqual(runner.call_args.kwargs['requests'], 10000)

    def test_failure_and_interruption_save_partial_report(self):
        for error, status in [(RuntimeError('server crashed'), 'failed'), (KeyboardInterrupt(), 'interrupted')]:
            with self.subTest(status=status), TemporaryDirectory() as workspace:
                with patch.object(x, 'bench', side_effect=[self.run_result(), error]) as runner:
                    with self.assertRaises(type(error)):
                        x.bench_compare('baseline', 'candidate', output_dir=workspace)
                report = json.loads(next(Path(workspace).glob('comparison-*/comparison.json')).read_text())
                self.assertEqual(report['status'], status)
                self.assertEqual(len(report['baseline_runs']), 1)
                self.assertEqual(report['comparisons'], [])
                self.assertEqual(runner.call_count, 2)

    def test_invalid_options_do_not_run_benchmarks(self):
        for options in ({'repeats': 2}, {'threshold': 0}, {'threshold': 101}, {'threshold': float('nan')},
                        {'threshold': float('inf')}, {'clients': 0}):
            with self.subTest(options=options), patch.object(x, 'bench') as runner:
                with self.assertRaises(RuntimeError):
                    x.bench_compare('baseline', 'candidate', **options)
                runner.assert_not_called()


@unittest.skipUnless(sys.platform.startswith('linux'), 'executable fixtures require Linux')
class BenchmarkProcessTest(unittest.TestCase):
    def test_real_process_output_capture_and_cleanup(self):
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            server = root / 'kvrocks'
            server.write_text(
                f'#!{sys.executable}\n'
                'import os, socket, sys\n'
                'port = int(sys.argv[sys.argv.index("--port") + 1])\n'
                'print(os.getpid(), flush=True)\n'
                'with socket.socket() as listener:\n'
                '    listener.bind(("127.0.0.1", port))\n'
                '    listener.listen()\n'
                '    while True:\n'
                '        connection, _ = listener.accept()\n'
                '        with connection:\n'
                '            connection.recv(1024)\n'
                '            connection.sendall(b"+PONG\\r\\n")\n'
            )
            benchmark = root / 'redis-benchmark'
            benchmark.write_text(
                f'#!{sys.executable}\nimport sys\n'
                f'print("redis-benchmark fixture" if "--version" in sys.argv else {CSV!r})\n'
            )
            server.chmod(0o700)
            benchmark.chmod(0o700)
            x.bench(str(root), str(benchmark), output_dir=str(root / 'results'))
            artifacts = next((root / 'results').iterdir())
            report = json.loads((artifacts / 'results.json').read_text())
            self.assertEqual(report['status'], 'success')
            self.assertEqual(len(report['results']), 2)
            server_pid = int((artifacts / 'server.log').read_text().strip())
            with self.assertRaises(ProcessLookupError):
                os.kill(server_pid, 0)

            comparison = subprocess.run(
                [sys.executable, '-B', str(Path(x.__file__)), 'bench-compare', str(root), str(root),
                 '--bench-path', str(benchmark), '--output-dir', str(root / 'results')],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(comparison.returncode, 0, comparison.stderr)
            summary = next((root / 'results').glob('comparison-*/comparison.json'))
            comparison_report = json.loads(summary.read_text())
            self.assertEqual(comparison_report['status'], 'success')
            self.assertEqual(len(comparison_report['baseline_runs']), 3)
            self.assertEqual(len(comparison_report['candidate_runs']), 3)
            for run in comparison_report['baseline_runs'] + comparison_report['candidate_runs']:
                self.assertTrue((Path(run['artifacts']) / 'results.json').is_file())


if __name__ == '__main__':
    unittest.main()
