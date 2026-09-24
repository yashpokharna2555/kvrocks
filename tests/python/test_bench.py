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
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location('kvrocks_x', Path(__file__).resolve().parents[2] / 'x.py')
x = importlib.util.module_from_spec(spec)
spec.loader.exec_module(x)


class BenchTest(unittest.TestCase):
    def setUp(self):
        self.server = MagicMock()
        self.server.poll.return_value = None
        self.benchmark = MagicMock()
        self.benchmark.poll.return_value = 0
        self.benchmark.returncode = 0
        self.popen = self.patch('Popen', side_effect=[self.server, self.benchmark])
        self.patch('find_command', return_value='/usr/bin/redis-benchmark')
        self.patch('Path.is_file', return_value=True)
        self.patch('time.sleep')
        self.clock = self.patch('time.monotonic', return_value=0)
        connection = self.patch('socket.create_connection')
        self.client = connection.return_value.__enter__.return_value
        self.client.recv.side_effect = [b'+PO', b'NG\r\n']

    def patch(self, name, **kwargs):
        patcher = patch.object(x, name, **kwargs)
        if '.' in name:
            owner, attribute = name.split('.')
            patcher = patch.object(getattr(x, owner), attribute, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_success_cleans_up_server_and_workspace(self):
        x.bench('build', 'redis-benchmark', [])
        self.client.sendall.assert_called_once_with(b'*1\r\n$4\r\nPING\r\n')
        self.server.terminate.assert_called_once()
        self.server.wait.assert_called_once_with(timeout=10)
        workspace = self.popen.call_args_list[0].kwargs['cwd']
        self.assertFalse(Path(workspace).exists())

    def test_missing_binary_does_not_start_server(self):
        with patch.object(x.Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'binary not found'):
                x.bench('missing', 'redis-benchmark', [])
        self.popen.assert_not_called()

    def test_extra_arguments_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Extra benchmark arguments'):
            x.bench('build', 'redis-benchmark', ['-n', '100'])
        self.popen.assert_not_called()

    def test_startup_failure_does_not_start_benchmark(self):
        self.server.poll.return_value = 1
        self.server.returncode = 1
        with self.assertRaisesRegex(RuntimeError, 'exited during startup'):
            x.bench('build', 'redis-benchmark', [])
        self.assertEqual(self.popen.call_count, 1)

    def test_startup_timeout_stops_server(self):
        self.clock.side_effect = [0, 31]
        with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
            x.bench('build', 'redis-benchmark', [])
        self.server.terminate.assert_called_once()

    def test_benchmark_failure_stops_server(self):
        self.benchmark.returncode = 1
        with self.assertRaisesRegex(RuntimeError, 'redis-benchmark failed'):
            x.bench('build', 'redis-benchmark', [])
        self.server.terminate.assert_called_once()

    def test_benchmark_timeout_stops_both_processes(self):
        self.benchmark.poll.return_value = None
        self.clock.side_effect = [0, 0, 0, 121]
        with self.assertRaisesRegex(RuntimeError, '120-second timeout'):
            x.bench('build', 'redis-benchmark', [])
        self.benchmark.terminate.assert_called_once()
        self.server.terminate.assert_called_once()

    def test_server_crash_stops_benchmark(self):
        self.server.poll.side_effect = [None, None, 1, 1]
        self.server.returncode = 1
        self.benchmark.poll.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'Kvrocks exited during benchmarking'):
            x.bench('build', 'redis-benchmark', [])
        self.benchmark.terminate.assert_called_once()

    def test_interruption_stops_both_processes(self):
        self.benchmark.poll.side_effect = [KeyboardInterrupt, None]
        with self.assertRaises(KeyboardInterrupt):
            x.bench('build', 'redis-benchmark', [])
        self.benchmark.terminate.assert_called_once()
        self.server.terminate.assert_called_once()

    def test_shutdown_timeout_kills_server(self):
        self.server.wait.side_effect = [x.TimeoutExpired('kvrocks', 10), 0]
        x.bench('build', 'redis-benchmark', [])
        self.server.kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
