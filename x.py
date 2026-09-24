#!/usr/bin/env python3

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

from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter, REMAINDER
import csv
import io
import json
import math
from glob import glob
import os
from pathlib import Path
import re
import filecmp
from subprocess import Popen, PIPE, STDOUT, TimeoutExpired, run as subprocess_run
import socket
from statistics import median
import sys
import time
from typing import List, Any, Optional, IO, Tuple
from shutil import which
from tempfile import TemporaryDirectory, mkdtemp

CMAKE_REQUIRE_VERSION = (3, 16, 0)
CLANG_FORMAT_REQUIRED_VERSION = (18, 0, 0)
CLANG_TIDY_REQUIRED_VERSION = (18, 0, 0)
GOLANGCI_LINT_REQUIRED_VERSION = (2, 13, 0)

SEMVER_REGEX = re.compile(
    r"""
        ^
        (?P<major>0|[1-9]\d*)
        \.
        (?P<minor>0|[1-9]\d*)
        \.
        (?P<patch>0|[1-9]\d*)
        (?:-(?P<prerelease>
            (?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)
            (?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*
        ))?
        (?:\+(?P<build>
            [0-9a-zA-Z-]+
            (?:\.[0-9a-zA-Z-]+)*
        ))?
        $
    """,
    re.VERBOSE,
)


# NOTE: the return type should be Popen[str], but Popen is not subscriptable before python 3.9
def run(*args: str, msg: Optional[str] = None, verbose: bool = False, **kwargs: Any) -> Popen:
    sys.stdout.flush()
    if verbose:
        print(f"$ {' '.join(args)}")

    p = Popen(args, **kwargs)
    code = p.wait()
    if code != 0:
        err = f"\nfailed to run: {args}\nexit with code: {code}\n"
        if msg:
            err += f"error message: {msg}\n"
        raise RuntimeError(err)

    return p


def run_pipe(*args: str, msg: Optional[str] = None, verbose: bool = False, **kwargs: Any) -> IO[str]:
    p = run(*args, msg=msg, verbose=verbose, stdout=PIPE, universal_newlines=True, **kwargs)
    return p.stdout # type: ignore


def find_command(command: str, msg: Optional[str] = None) -> str:
    return run_pipe("which", command, msg=msg).read().strip()


def check_version(current: str, required: Tuple[int, int, int], prog_name: Optional[str] = None) -> Tuple[
    int, int, int]:
    require_version = '.'.join(map(str, required))
    semver_match = SEMVER_REGEX.match(current)
    if semver_match is None:
        raise RuntimeError(f"{prog_name} {require_version} or higher is required, got: {current}")
    semver_dict = semver_match.groupdict()
    semver = (int(semver_dict["major"]), int(semver_dict["minor"]), int(semver_dict["patch"]))
    if semver < required:
        raise RuntimeError(f"{prog_name} {require_version} or higher is required, got: {current}")

    return semver

def prepare() -> None:
    basedir = Path(__file__).parent.absolute()
    
    # Install Git hooks
    hooks = basedir / "dev" / "hooks"
    git_hooks = basedir / ".git" / "hooks"

    git_hooks.mkdir(exist_ok=True)
    for hook in hooks.iterdir():
        dst = git_hooks / hook.name
        if dst.exists():
            if filecmp.cmp(hook, dst, shallow=False):
                print(f"{hook.name} already installed.")
                continue
            raise RuntimeError(f"{dst} already exists; please remove it first")
        else:
            dst.symlink_to(hook)
            print(f"{hook.name} installed at {dst}.")

def build(dir: str, jobs: Optional[int] = None, ninja: bool = False, unittest: bool = False,
          compiler: str = 'auto', cmake_path: str = 'cmake', D: List[str] = [], skip_build: bool = False,
          dep_dir: Optional[str] = None, toolchain: Optional[str] = None) -> None:
    basedir = Path(__file__).parent.absolute()

    find_command("autoconf", msg="autoconf is required to build jemalloc")
    cmake = find_command(cmake_path, msg="CMake is required")

    output = run_pipe(cmake, "-version")
    output = run_pipe("head", "-n", "1", stdin=output)
    output = run_pipe("awk", "{print $(NF)}", stdin=output)
    cmake_version = output.read().strip()
    check_version(cmake_version, CMAKE_REQUIRE_VERSION, "CMake")

    os.makedirs(dir, exist_ok=True)

    cmake_options = ["-DCMAKE_BUILD_TYPE=RelWithDebInfo"]
    if toolchain:
       cmake_options.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain}")
    if ninja:
        cmake_options.append("-G Ninja")
    if compiler == 'gcc':
        cmake_options += ["-DCMAKE_C_COMPILER=gcc", "-DCMAKE_CXX_COMPILER=g++"]
    elif compiler == 'clang':
        cmake_options += ["-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++"]
    if D:
        cmake_options += [f"-D{o}" for o in D]
    if dep_dir:
        dep_dir = os.path.abspath(dep_dir)
        cmake_options += [f"-DDEPS_FETCH_DIR={dep_dir}"]

    run(cmake, str(basedir), *cmake_options, verbose=True, cwd=dir)

    if skip_build:
        return

    target = ["kvrocks", "kvrocks2redis"]
    if unittest:
        target.append("unittest")

    options = ["--build", "."]
    if jobs:
        options.append(f"-j{jobs}")
    options += ["-t", *target]

    run(cmake, *options, verbose=True, cwd=dir)


def fetch_deps(dir: str, D: List[str] = []) -> None:
    dir = os.path.abspath(dir)
    with TemporaryDirectory(prefix="kvrocks-fetch-deps-") as build_dir:
        build(build_dir, D=D, dep_dir=dir, skip_build=True)


def get_source_files(dir: Path) -> List[str]:
    return [
        *glob(str(dir / "src/**/*.h"), recursive=True),
        *glob(str(dir / "src/**/*.cc"), recursive=True),
        *glob(str(dir / "tests/cppunit/**/*.h"), recursive=True),
        *glob(str(dir / "tests/cppunit/**/*.cc"), recursive=True),
        *glob(str(dir / "utils/kvrocks2redis/**/*.h"), recursive=True),
        *glob(str(dir / "utils/kvrocks2redis/**/*.cc"), recursive=True),
    ]


def clang_format(clang_format_path: str, fix: bool = False) -> None:
    command = find_command(clang_format_path, msg="clang-format is required")

    version_res = run_pipe(command, '--version').read().strip()
    version_re_res = re.search(r'version\s+((?:\w|\.)+)', version_res)
    if version_re_res:
        version_str = version_re_res.group(1)
    else:
        raise RuntimeError(f"version not found in `{command} --version`")

    check_version(version_str, CLANG_FORMAT_REQUIRED_VERSION, "clang-format")

    basedir = Path(__file__).parent.absolute()
    sources = get_source_files(basedir)

    if fix:
        options = ['-i']
    else:
        options = ['--dry-run', '--Werror']

    run(command, *options, *sources, verbose=True, cwd=basedir)


def clang_tidy(dir: str, jobs: Optional[int], clang_tidy_path: str, run_clang_tidy_path: str, fix: bool) -> None:
    # use the run-clang-tidy Python script provided by LLVM Clang
    run_command = find_command(run_clang_tidy_path, msg="run-clang-tidy is required")
    tidy_command = find_command(clang_tidy_path, msg="clang-tidy is required")

    version_res = run_pipe(tidy_command, '--version').read().strip()
    version_re_res = re.search(r'version\s+((?:\w|\.)+)', version_res)
    if version_re_res:
        version_str = version_re_res.group(1)
    else:
        raise RuntimeError(f"version not found in `{tidy_command} --version`")

    check_version(version_str, CLANG_TIDY_REQUIRED_VERSION, "clang-tidy")

    if not (Path(dir) / 'compile_commands.json').exists():
        raise RuntimeError(f"expect compile_commands.json in build directory {dir}")

    basedir = Path(__file__).parent.absolute()

    options = ['-p', dir, '-clang-tidy-binary', tidy_command]
    if jobs is not None:
        options.append(f'-j{jobs}')

    options.extend(['-fix'] if fix else [])

    regexes = ['kvrocks/src/', 'utils/kvrocks2redis/', 'tests/cppunit/']

    options.append(f'-header-filter={"|".join(regexes)}')

    run(run_command, *options, *regexes, verbose=True, cwd=basedir)


def golangci_lint(golangci_lint_path: str) -> None:
    def get_gopath() -> Tuple[Path, Path]:
        go = find_command('go', msg='go is required for testing')
        gopath = run_pipe(go, 'env', 'GOPATH').read().strip()
        bindir = Path(gopath).absolute() / 'bin'
        binpath = bindir / 'golangci-lint'
        return bindir, binpath

    def get_syspath(sys_path: str) -> Tuple[str, str]:
        golangci_command = find_command(sys_path, msg="golangci-lint is required")
        version_res = run_pipe(golangci_command, '--version').read().strip()
        version_re_res = re.search(r'version\s+((?:\w|\.)+)', version_res)
        if version_re_res:
            version_str = version_re_res.group(1)
        else:
            raise RuntimeError(f"version not found in `{golangci_command} --version`")
        return golangci_command, version_str

    def download_package(bindir: str) -> None:
        output = run_pipe('curl', '-sfL', 'https://golangci-lint.run/install.sh',
                            verbose=True)
        version_str = 'v' + '.'.join(map(str, GOLANGCI_LINT_REQUIRED_VERSION))
        run('sh', '-s', '--', '-b', bindir, version_str, verbose=True, stdin=output)

    if which(golangci_lint_path) is None:
        bindir, binpath = get_gopath()
        binpath_str = str(binpath)
        if not binpath.exists():
            download_package(str(bindir))

    else:
        binpath_str, version_str = get_syspath(golangci_lint_path)
        check_version(version_str, GOLANGCI_LINT_REQUIRED_VERSION, "golangci-lint")

    basedir = Path(__file__).parent.absolute() / 'tests' / 'gocase'
    run(binpath_str, 'run', '-v', './...', cwd=str(basedir), verbose=True)


def write_version(release_version: str) -> str:
    version = release_version.strip()
    if SEMVER_REGEX.match(version) is None:
        raise RuntimeError(f"Kvrocks version should follow semver spec, got: {version}")

    with open('src/VERSION.txt', 'w+') as f:
        f.write(version)

    return version


def package_source(release_version: str, release_candidate_number: Optional[int]) -> None:
    # 0. Write input version to VERSION file
    version = write_version(release_version)

    # 1. Git commit and tag
    git = find_command('git', msg='git is required for source packaging')
    run(git, 'commit', '-a', '-m', f'release: prepare source release apache-kvrocks-{version}')
    if release_candidate_number is None:
        run(git, 'tag', '-a', f'v{version}', '-m', f'release: copy for tag v{version}')
    else:
        run(git, 'tag', '-a', f'v{version}-rc{release_candidate_number}', '-m', f'release: copy for tag v{version}-rc{release_candidate_number}')

    # 2. Create the source tarball
    folder = f'apache-kvrocks-{version}-src'
    tarball = f'apache-kvrocks-{version}-src.tar.gz'
    run(git, 'archive', '--format=tar.gz', f'--output={tarball}', f'--prefix={folder}/', 'HEAD')

    # 3. GPG Sign
    gpg = find_command('gpg', msg='gpg is required for source packaging')
    run(gpg, '--detach-sign', '--armor', tarball)

    # 4. Generate sha512 checksum
    shasum = find_command('shasum', msg='shasum is required for source packaging')
    with open(f'{tarball}.sha512', 'w+') as f:
        run(shasum, '-a', '512', tarball, stdout=f)


def test_cpp(dir: str, rest: List[str]) -> None:
    basedir = Path(dir).absolute()
    unittest = basedir / 'unittest'

    run(str(unittest), *rest, cwd=str(basedir), verbose=True)


def test_go(dir: str, cli_path: str, rest: List[str]) -> None:
    go = find_command('go', msg='go is required for testing')
    find_command(cli_path, msg='redis-cli is required for testing')

    binpath = Path(dir).absolute() / 'kvrocks'
    basedir = Path(__file__).parent.absolute() / 'tests' / 'gocase'
    workspace = basedir / 'workspace'

    args = [
        'test', '-timeout=1800s', '-bench=.', './...',
        f'-binPath={binpath}',
        f'-cliPath={cli_path}',
        f'-workspace={workspace}',
        *rest
    ]

    run(go, *args, cwd=str(basedir), verbose=True)


def parse_benchmark_results(output: str) -> List[dict]:
    columns = ['test', 'rps', 'avg_latency_ms', 'min_latency_ms', 'p50_latency_ms',
               'p95_latency_ms', 'p99_latency_ms', 'max_latency_ms']
    reader = csv.reader(io.StringIO(output), strict=True)
    results = []
    seen = set()
    try:
        if next(reader, None) != columns:
            raise ValueError('expected the redis-benchmark 6.2+ CSV header')
        for row in reader:
            if not row:
                continue
            if len(row) != len(columns) or row[0] not in ('SET', 'GET') or row[0] in seen:
                raise ValueError('unexpected or duplicate benchmark row')
            measurements = dict(zip(columns[1:], map(float, row[1:])))
            if any(not math.isfinite(value) or value < 0 for value in measurements.values()):
                raise ValueError('measurements must be finite and nonnegative')
            if measurements['rps'] == 0:
                raise ValueError('throughput must be positive')
            results.append({'command': row[0], **measurements})
            seen.add(row[0])
        if seen != {'SET', 'GET'}:
            raise ValueError('missing SET or GET measurements')
    except (ValueError, csv.Error) as error:
        raise RuntimeError(f'Invalid benchmark output: {error}') from error
    return results


def bench(dir: str, bench_path: str, output_dir: str = 'benchmark-results',
          requests: int = 10000, clients: int = 10, data_size: int = 3,
          pipeline: int = 1) -> dict:
    settings = {'requests': requests, 'clients': clients, 'data_size': data_size, 'pipeline': pipeline}
    if any(value <= 0 or value > 2147483647 for value in settings.values()):
        raise RuntimeError('Benchmark settings must be positive 32-bit integers')

    output_root = Path(output_dir).absolute()
    output_root.mkdir(parents=True, exist_ok=True)
    artifacts = Path(mkdtemp(prefix='run-', dir=str(output_root)))
    report = {'schema_version': 1, 'status': 'failed', 'settings': settings,
              'kvrocks': str(Path(dir).absolute() / 'kvrocks'), 'artifacts': str(artifacts), 'results': []}
    print(f'Benchmark artifacts: {artifacts}', flush=True)
    try:
        with (artifacts / 'server.log').open('w', encoding='utf-8') as server_log, \
                (artifacts / 'benchmark.csv').open('w', encoding='utf-8') as output, \
                (artifacts / 'benchmark.stderr.log').open('w', encoding='utf-8') as errors:
            run_benchmark(dir, bench_path, settings, server_log, output, errors, report)
        diagnostics = (artifacts / 'benchmark.stderr.log').read_text(encoding='utf-8', errors='replace')
        if re.search(r'error|failed|could not|disconnected|aborting', diagnostics, re.IGNORECASE):
            raise RuntimeError('redis-benchmark reported errors; see benchmark.stderr.log')
        report['results'] = parse_benchmark_results((artifacts / 'benchmark.csv').read_text(encoding='utf-8'))
        report['status'] = 'success'
    except BaseException as error:
        report['status'] = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
        report['error'] = str(error) or type(error).__name__
        raise
    finally:
        (artifacts / 'results.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    for result in report['results']:
        print(f"{result['command']}: {result['rps']:.2f} requests/s, p50 {result['p50_latency_ms']:.3f} ms")
    return report


def compare_benchmark_results(baseline: List[dict], candidate: List[dict], threshold: float) -> List[dict]:
    if not baseline or len(baseline) != len(candidate):
        raise RuntimeError('Comparison requires equal, nonempty sets of benchmark runs')
    reference = baseline[0]
    for run in baseline + candidate:
        if run['status'] != 'success':
            raise RuntimeError('Cannot compare failed benchmark runs')
        if run['settings'] != reference['settings'] or run['benchmark_version'] != reference['benchmark_version']:
            raise RuntimeError('Comparison requires identical workloads and benchmark versions')

    comparisons = []
    for command in ('SET', 'GET'):
        groups = [[next(result for result in run['results'] if result['command'] == command)
                   for run in runs] for runs in (baseline, candidate)]
        for metric in ('rps', 'avg_latency_ms', 'p50_latency_ms', 'p95_latency_ms', 'p99_latency_ms'):
            before, after = [median(result[metric] for result in group) for group in groups]
            change = 100 * (after - before) / before if before else None
            degradation = (-change if metric == 'rps' else change) if change is not None else None
            regression = degradation is not None and (
                degradation >= threshold or math.isclose(degradation, threshold, rel_tol=1e-12)
            )
            comparisons.append({'command': command, 'metric': metric, 'baseline_median': before,
                                'candidate_median': after, 'change_percent': change, 'regression': regression})
    return comparisons


def bench_compare(baseline_dir: str, candidate_dir: str, bench_path: str = 'redis-benchmark',
                  output_dir: str = 'benchmark-results', requests: int = 10000, clients: int = 10,
                  data_size: int = 3, pipeline: int = 1, repeats: int = 3, threshold: float = 20) -> None:
    if repeats < 3:
        raise RuntimeError('Comparison requires at least 3 measured runs per build')
    if not math.isfinite(threshold) or threshold <= 0 or threshold > 100:
        raise RuntimeError('Regression threshold must be greater than 0 and at most 100 percent')
    settings = {'requests': requests, 'clients': clients, 'data_size': data_size, 'pipeline': pipeline}
    if any(value <= 0 or value > 2147483647 for value in settings.values()):
        raise RuntimeError('Benchmark settings must be positive 32-bit integers')

    output_root = Path(output_dir).absolute()
    output_root.mkdir(parents=True, exist_ok=True)
    artifacts = Path(mkdtemp(prefix='comparison-', dir=str(output_root)))
    report = {'schema_version': 1, 'status': 'failed', 'repeats': repeats, 'threshold_percent': threshold,
              'settings': settings, 'baseline_dir': str(Path(baseline_dir).absolute()),
              'candidate_dir': str(Path(candidate_dir).absolute()), 'baseline_runs': [], 'candidate_runs': [],
              'comparisons': []}
    print(f'Comparison artifacts: {artifacts}', flush=True)
    try:
        for iteration in range(repeats):
            # Reverse each pair to reduce systematic bias from always running one build first.
            order = ('baseline', 'candidate') if iteration % 2 == 0 else ('candidate', 'baseline')
            for role in order:
                print(f'{role}: measured run {iteration + 1}/{repeats}', flush=True)
                result = bench(report[f'{role}_dir'], bench_path,
                               output_dir=str(artifacts / role), **settings)
                report[f'{role}_runs'].append(result)
        report['comparisons'] = compare_benchmark_results(report['baseline_runs'], report['candidate_runs'], threshold)
        report['status'] = 'warning' if any(item['regression'] for item in report['comparisons']) else 'success'
    except BaseException as error:
        report['status'] = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
        report['error'] = str(error) or type(error).__name__
        raise
    finally:
        (artifacts / 'comparison.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')

    for item in report['comparisons']:
        change = item['change_percent']
        change_text = f'{change:+.2f}%' if change is not None else 'unavailable (zero baseline)'
        prefix = 'WARNING: ' if item['regression'] else ''
        print(f"{prefix}{item['command']} {item['metric']}: "
              f"{item['baseline_median']:.3f} -> {item['candidate_median']:.3f}, change {change_text}")
    print(f"Comparison completed: {report['status']}. Report: {artifacts / 'comparison.json'}")


def run_benchmark(dir: str, bench_path: str, settings: dict, server_log: IO[str],
                  output: IO[str], errors: IO[str], report: dict) -> None:

    benchmark = find_command(bench_path, msg='redis-benchmark is required for benchmarking')
    binpath = Path(dir).absolute() / 'kvrocks'
    if not binpath.is_file():
        raise RuntimeError(f"kvrocks binary not found: {binpath}")
    version = subprocess_run([benchmark, '--version'], capture_output=True, text=True, check=True, timeout=10)
    report['benchmark_version'] = version.stdout.strip()

    host = '127.0.0.1'
    # The reservation must be released before Kvrocks can bind this port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind((host, 0))
        port = reservation.getsockname()[1]

    def stop_process(process: Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except TimeoutExpired:
            process.kill()
            process.wait()

    with TemporaryDirectory(prefix='kvrocks-bench-') as workspace:
        server_args = [
            str(binpath),
            '--bind', host,
            '--port', str(port),
            '--dir', workspace,
            '--pidfile', str(Path(workspace) / 'kvrocks.pid'),
            '--log-dir', 'stdout',
            '--daemonize', 'no',
            '--supervised', 'no',
        ]

        print(f"Starting Kvrocks on {host}:{port}", flush=True)
        server = Popen(server_args, cwd=workspace, stdout=server_log, stderr=STDOUT)
        benchmark_process = None

        try:
            startup_deadline = time.monotonic() + 30
            while True:
                if server.poll() is not None:
                    raise RuntimeError(f"Kvrocks exited during startup: {server.returncode}")
                if time.monotonic() >= startup_deadline:
                    raise RuntimeError("Kvrocks did not become ready within 30 seconds")

                try:
                    with socket.create_connection((host, port), timeout=1) as client:
                        client.sendall(b'*1\r\n$4\r\nPING\r\n')
                        response = b''
                        while len(response) < 7:
                            chunk = client.recv(7 - len(response))
                            if not chunk:
                                break
                            response += chunk
                        if response == b'+PONG\r\n':
                            break
                except OSError:
                    pass
                time.sleep(0.1)

            if server.poll() is not None:
                raise RuntimeError("Kvrocks exited before the benchmark started")

            benchmark_args = [
                benchmark,
                '-h', host,
                '-p', str(port),
                '-t', 'set,get',
                '-n', str(settings['requests']),
                '-c', str(settings['clients']),
                '-d', str(settings['data_size']),
                '-P', str(settings['pipeline']),
                '--csv',
            ]
            report['benchmark_command'] = benchmark_args
            print(f"Running: {' '.join(benchmark_args)}", flush=True)
            benchmark_process = Popen(benchmark_args, stdout=output, stderr=errors)
            benchmark_deadline = time.monotonic() + 120
            while benchmark_process.poll() is None:
                if server.poll() is not None:
                    raise RuntimeError(f"Kvrocks exited during benchmarking: {server.returncode}")
                if time.monotonic() >= benchmark_deadline:
                    raise RuntimeError("Benchmark exceeded its 120-second timeout")
                time.sleep(0.1)

            if benchmark_process.returncode != 0:
                raise RuntimeError(f"redis-benchmark failed: {benchmark_process.returncode}")
            if server.poll() is not None:
                raise RuntimeError(f"Kvrocks exited during benchmarking: {server.returncode}")
        finally:
            try:
                if benchmark_process is not None:
                    stop_process(benchmark_process)
            finally:
                stop_process(server)


if __name__ == '__main__':
    parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
    parser.set_defaults(func=parser.print_help)

    subparsers = parser.add_subparsers()

    parser_format = subparsers.add_parser(
        'format',
        description="Format source code",
        help="Format source code")
    parser_format.set_defaults(func=lambda **args: clang_format(**args, fix=True))
    parser_format.add_argument('--clang-format-path', default='clang-format',
                               help="path of clang-format used to check source")

    parser_check = subparsers.add_parser(
        'check',
        description="Check or lint source code",
        help="Check or lint source code")
    parser_check.set_defaults(func=parser_check.print_help)
    parser_check_subparsers = parser_check.add_subparsers()
    parser_check_format = parser_check_subparsers.add_parser(
        'format',
        description="Check source format by clang-format",
        help="Check source format by clang-format")
    parser_check_format.set_defaults(func=lambda **args: clang_format(**args, fix=False))
    parser_check_format.add_argument('--clang-format-path', default='clang-format',
                                     help="path of clang-format used to check source")
    parser_check_tidy = parser_check_subparsers.add_parser(
        'tidy',
        description="Check code with clang-tidy",
        help="Check code with clang-tidy",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser_check_tidy.set_defaults(func=clang_tidy)
    parser_check_tidy.add_argument('dir', metavar='BUILD_DIR', nargs='?', default='build',
                                   help="directory to store cmake-generated and build files")
    parser_check_tidy.add_argument('-j', '--jobs', metavar='N', help='execute N build jobs concurrently')
    parser_check_tidy.add_argument('--clang-tidy-path', default='clang-tidy',
                                   help="path of clang-tidy used to check source")
    parser_check_tidy.add_argument('--run-clang-tidy-path', default='run-clang-tidy',
                                   help="path of run-clang-tidy used to check source")
    parser_check_tidy.add_argument('--fix', default=False, action='store_true',
                              help='automatically fix codebase via clang-tidy suggested changes')
    parser_check_golangci_lint = parser_check_subparsers.add_parser(
        'golangci-lint',
        description="Check code with golangci-lint (https://golangci-lint.run/)",
        help="Check code with golangci-lint (https://golangci-lint.run/)",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser_check_golangci_lint.set_defaults(func=golangci_lint)
    parser_check_golangci_lint.add_argument('--golangci-lint-path', default='golangci-lint',
                                   help="path of golangci-lint used to check source")
    
    parser_build = subparsers.add_parser(
        'build',
        description="Build executables to BUILD_DIR [default: build]",
        help="Build executables to BUILD_DIR [default: build]",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser_build.add_argument('dir', metavar='BUILD_DIR', nargs='?', default='build',
                              help="directory to store cmake-generated and build files")
    parser_build.add_argument('-j', '--jobs', metavar='N', type=int, help='execute N build jobs concurrently')
    parser_build.add_argument('--ninja', default=False, action='store_true', help='use Ninja to build kvrocks')
    parser_build.add_argument('--unittest', default=False, action='store_true', help='build unittest target')
    parser_build.add_argument('--compiler', default='auto', choices=('auto', 'gcc', 'clang'),
                              help="compiler used to build kvrocks")
    parser_build.add_argument('--toolchain', metavar='FILE', help="path to CMake toolchain file for cross-compiling")
    parser_build.add_argument('--cmake-path', default='cmake', help="path of cmake binary used to build kvrocks")
    parser_build.add_argument('-D', action='append', metavar='key=value', help='extra CMake definitions')
    parser_build.add_argument('--skip-build', default=False, action='store_true',
                              help='runs only the configure stage, skip the build stage')
    parser_build.add_argument('--dep-dir', help='directory to store fetched archives of dependencies')
    parser_build.set_defaults(func=build)

    parser_fetch_deps = subparsers.add_parser(
        'fetch-deps',
        description="Fetch dependency archives to DEP_DIR [default: build-deps]",
        help="Fetch dependency archives to DEP_DIR [default: build-deps]",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser_fetch_deps.add_argument('dir', metavar='DEP_DIR', nargs='?', default='build-deps',
                              help="directory to store fetched archives of dependencies")
    parser_fetch_deps.add_argument('-D', action='append', metavar='key=value',
                              help='extra CMake definitions used to determine fetched dependencies')
    parser_fetch_deps.set_defaults(func=fetch_deps)

    parser_package = subparsers.add_parser(
        'package',
        description="Package the source tarball or binary installer",
        help="Package the source tarball or binary installer",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser_package.set_defaults(func=parser_package.print_help)
    parser_package_subparsers = parser_package.add_subparsers()
    parser_package_source = parser_package_subparsers.add_parser(
        'source',
        description="Package the source tarball",
        help="Package the source tarball",
    )
    parser_package_source.add_argument('-v', '--release-version', required=True, metavar='VERSION',
                                       help='current releasing version')
    parser_package_source.add_argument('-rc', '--release-candidate-number',required=False, type=int, help='current releasing candidate number')
    parser_package_source.set_defaults(func=package_source)

    parser_test = subparsers.add_parser(
        'test',
        description="Test against a specific kvrocks build",
        help="Test against a specific kvrocks build",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser_test.set_defaults(func=parser_test.print_help)
    parser_test_subparsers = parser_test.add_subparsers()

    parser_test_cpp = parser_test_subparsers.add_parser(
        'cpp',
        description="Test kvrocks via cpp unit tests",
        help="Test kvrocks via cpp unit tests",
    )
    parser_test_cpp.add_argument('dir', metavar='BUILD_DIR', nargs='?', default='build',
                                 help="directory including kvrocks build files")
    parser_test_cpp.add_argument('rest', nargs=REMAINDER, help="the rest of arguments to forward to cpp unittest")
    parser_test_cpp.set_defaults(func=test_cpp)

    parser_test_go = parser_test_subparsers.add_parser(
        'go',
        description="Test kvrocks via go test cases",
        help="Test kvrocks via go test cases",
    )
    parser_test_go.add_argument('dir', metavar='BUILD_DIR', nargs='?', default='build',
                                help="directory including kvrocks build files")
    parser_test_go.add_argument('--cli-path', default='redis-cli', help="path of redis-cli to test kvrocks")
    parser_test_go.add_argument('rest', nargs=REMAINDER, help="the rest of arguments to forward to go test")
    parser_test_go.set_defaults(func=test_go)

    parser_bench = subparsers.add_parser(
        'bench',
        description="Run redis-benchmark against a specific kvrocks build",
        help="Run redis-benchmark against a specific kvrocks build",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser_bench.add_argument('dir', metavar='BUILD_DIR', nargs='?', default='build',
                             help="directory including kvrocks build files")
    parser_bench.add_argument('--bench-path', default='redis-benchmark',
                             help="path of redis-benchmark used to bench kvrocks")
    parser_bench.add_argument('--output-dir', default='benchmark-results', help="directory for benchmark artifacts")
    parser_bench.add_argument('--requests', type=int, default=10000, help="requests per command")
    parser_bench.add_argument('--clients', type=int, default=10, help="concurrent clients")
    parser_bench.add_argument('--data-size', type=int, default=3, help="value size in bytes")
    parser_bench.add_argument('--pipeline', type=int, default=1, help="requests per pipeline")
    parser_bench.set_defaults(func=bench)

    parser_compare = subparsers.add_parser(
        'bench-compare', description="Compare two Kvrocks builds using repeated benchmark runs",
        help="Compare two Kvrocks builds", formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser_compare.add_argument('baseline_dir', metavar='BASELINE_BUILD_DIR')
    parser_compare.add_argument('candidate_dir', metavar='CANDIDATE_BUILD_DIR')
    parser_compare.add_argument('--bench-path', default='redis-benchmark', help="path of redis-benchmark")
    parser_compare.add_argument('--output-dir', default='benchmark-results', help="directory for comparison artifacts")
    parser_compare.add_argument('--requests', type=int, default=10000, help="requests per command")
    parser_compare.add_argument('--clients', type=int, default=10, help="concurrent clients")
    parser_compare.add_argument('--data-size', type=int, default=3, help="value size in bytes")
    parser_compare.add_argument('--pipeline', type=int, default=1, help="requests per pipeline")
    parser_compare.add_argument('--repeats', type=int, default=3, help="measured runs per build (minimum 3)")
    parser_compare.add_argument('--threshold', type=float, default=20,
                                help="minimum throughput decrease or latency increase to warn about, in percent")
    parser_compare.set_defaults(func=bench_compare)

    parser_prepare = subparsers.add_parser(
        'prepare',
        description="Prepare scripts such as git hooks",
        help="Prepare scripts such as git hooks"
    )
    parser_prepare.set_defaults(func=prepare)

    args = parser.parse_args()

    arg_dict = dict(vars(args))
    del arg_dict['func']
    args.func(**arg_dict)
