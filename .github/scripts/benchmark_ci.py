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

from argparse import ArgumentParser
import json
import os
from pathlib import Path
import re
import subprocess


def git(*args):
    return subprocess.run(['git', *args], capture_output=True, text=True, timeout=60)


def select_revisions(event_name, event):
    head = git('rev-parse', 'HEAD')
    head.check_returncode()
    candidate = head.stdout.strip()
    reason = ''
    if event_name == 'pull_request':
        baseline = event['pull_request']['base']['sha']
        if candidate != event['pull_request']['head']['sha']:
            raise RuntimeError('Checked-out candidate does not match the pull request head')
    elif event_name == 'push':
        baseline = event.get('before', '')
        if candidate != event['after']:
            raise RuntimeError('Checked-out candidate does not match the pushed commit')
    elif event_name == 'workflow_dispatch':
        parent = git('rev-parse', '--verify', 'HEAD^')
        baseline = parent.stdout.strip() if parent.returncode == 0 else ''
    else:
        raise RuntimeError(f'Unsupported benchmark event: {event_name}')

    if not baseline or baseline == '0' * 40:
        baseline = ''
        reason = 'No previous commit is available; measuring the candidate only.'
    else:
        if not re.fullmatch(r'[0-9a-f]{40}', baseline):
            raise RuntimeError('Baseline must be a full commit SHA')
        present = git('cat-file', '-e', f'{baseline}^{{commit}}').returncode == 0
        if not present:
            try:
                fetched = git('fetch', '--no-tags', '--depth=1', 'origin', baseline)
                present = fetched.returncode == 0 and git('cat-file', '-e', f'{baseline}^{{commit}}').returncode == 0
            except subprocess.TimeoutExpired:
                present = False
        if not present:
            reason = f'Baseline {baseline} could not be fetched; measuring the candidate only.'
            baseline = ''
    return {'event': event_name, 'candidate': candidate, 'baseline': baseline, 'skip_reason': reason}


def prepare(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    revisions = select_revisions(os.environ['GITHUB_EVENT_NAME'], event)
    (output_dir / 'revisions.json').write_text(json.dumps(revisions, indent=2) + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f"baseline={revisions['baseline']}\n")


def render_summary(output_dir, outcome, job_status):
    lines = ['## Kvrocks benchmark', '', f'Benchmark step: **{outcome or "not run"}**. Job: **{job_status}**.', '']
    revisions_path = output_dir / 'revisions.json'
    if revisions_path.exists():
        revisions = json.loads(revisions_path.read_text())
        lines += [f"Candidate: `{revisions['candidate']}`", '',
                  f"Baseline: `{revisions['baseline'] or 'unavailable'}`", '']
        if revisions['skip_reason']:
            lines += [revisions['skip_reason'], '']

    comparisons = sorted(output_dir.glob('comparison-*/comparison.json'))
    if comparisons:
        report = json.loads(comparisons[-1].read_text())
        lines += [f"Comparison: **{report['status']}**. Warning threshold: {report['threshold_percent']}%.", '',
                  f"Measured runs per build: {report['repeats']}. Workload: `{json.dumps(report['settings'], sort_keys=True)}`.", '',
                  '| Command | Metric | Baseline median | Candidate median | Change | Verdict |',
                  '| --- | --- | ---: | ---: | ---: | --- |']
        for item in report['comparisons']:
            change = item['change_percent']
            delta = f'{change:+.2f}%' if change is not None else 'N/A (zero baseline)'
            verdict = 'WARNING' if item['regression'] else ('N/A' if change is None else 'OK')
            lines.append(f"| {item['command']} | {item['metric']} | {item['baseline_median']:.3f} | "
                         f"{item['candidate_median']:.3f} | {delta} | {verdict} |")
        if report['status'] == 'warning':
            print('::warning::Kvrocks benchmark detected a performance regression. See the job summary and artifacts.')
        if report.get('error'):
            lines += ['', 'Comparison failed or was interrupted. See comparison.json and logs for details.']
    else:
        runs = sorted(output_dir.glob('run-*/results.json'))
        if runs:
            report = json.loads(runs[-1].read_text())
            lines += [f"Candidate measurement: **{report['status']}**. No regression verdict is available.", '',
                      f"Workload: `{json.dumps(report['settings'], sort_keys=True)}`.", '',
                      '| Command | Requests/s | p50 latency (ms) |', '| --- | ---: | ---: |']
            for result in report['results']:
                lines.append(f"| {result['command']} | {result['rps']:.2f} | {result['p50_latency_ms']:.3f} |")
        else:
            lines += ['No measurements were produced. Check the setup/build steps and uploaded logs.']
    lines += ['', 'Throughput is requests/second; latency metrics are milliseconds. '
              'Performance warnings do not fail the job. Logs and JSON results are attached as artifacts.', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'summary'))
    parser.add_argument('--output-dir', type=Path, default=Path('benchmark-results'))
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.output_dir)
    else:
        summary = render_summary(args.output_dir, os.environ.get('BENCHMARK_OUTCOME', ''),
                                 os.environ.get('BENCHMARK_JOB_STATUS', 'unknown'))
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
            output.write(summary)
