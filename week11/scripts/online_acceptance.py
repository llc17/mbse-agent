"""Execute the frozen public batch, with explicitly scripted test confirmations."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from week11.scripts.audit_version import audit
from week11.scripts.cli_smoke import RecordedRuntime
from week11.src.cli import CLI
from week11.src.store import Store, WEEK11_ROOT


class ObservedCLI(CLI):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []

    async def _call(self, name, arguments):
        result = await super()._call(name, arguments)
        self.events.append({'name': name, 'arguments': deepcopy(arguments), 'result': deepcopy(result)})
        return result


def save(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def expected_parameters(case_id: str) -> dict[str, float]:
    if case_id == 'dual_public':
        return dict(heat_capacity_a=300000, heat_capacity_b=200000, shared_resistance=.05,
                    outdoor_resistance_a=.03, outdoor_resistance_b=.04,
                    initial_temperature_a=293.15, initial_temperature_b=293.15, outdoor_temperature=308.15)
    if case_id.startswith('rlc'):
        return dict(resistance=100, inductance=.01, cutoff_frequency=10000, input_voltage=5,
                    capacitance=1 / ((2 * math.pi * 10000)**2 * .01))
    cutoff = 2000 if case_id == 'rc_frequency_change' else 1000
    return dict(resistance=1000, cutoff_frequency=cutoff,
                input_voltage=6 if case_id == 'rc_voltage_change' else 5,
                capacitance=1 / (2 * math.pi * 1000 * cutoff))


async def execute_fixture(cli: ObservedCLI, store: Store) -> dict:
    proposal_id = cli.last_proposal_id
    if not proposal_id:
        raise ValueError('no valid proposal for scripted confirmation')
    with patch('builtins.input', return_value='CONFIRM ' + proposal_id):
        await cli._command('/confirm ' + proposal_id)
    task = await cli._call('execution_status', {'task_id': cli.last_task_id})
    if task.get('task', {}).get('state') != 'succeeded':
        raise ValueError('confirmed test task failed: ' + json.dumps(task, ensure_ascii=False))
    project = store.current_project(cli.session_id)
    version = store.version(project['project_id'], task['task']['version_id'])
    report = audit(WEEK11_ROOT / version['directory'], version['manifest_sha256'])
    if not all(report[key] for key in ('technical', 'binding', 'evidence')):
        raise ValueError('artifact audit failed: ' + json.dumps(report, ensure_ascii=False))
    return {'task': task['task'], 'audit': report}


async def seed_baseline(cli: ObservedCLI, store: Store, canonical: dict) -> dict:
    spec = deepcopy(canonical)
    replacements = {}
    for source in spec['sources']:
        new = store.record_source(cli.session_id, source['text'])
        replacements[source['id']] = new['source_id']
        source['id'] = new['source_id']
    for item in spec['parameters'] + spec['derivations'] + [spec['stimulus']]:
        item['source_id'] = replacements[item['source_id']]
    await cli._command('/proposal ' + json.dumps(spec, ensure_ascii=False))
    return await execute_fixture(cli, store)


async def run_case(root: Path, case: dict, repeat: int, baseline: dict) -> dict:
    root.mkdir(parents=True, exist_ok=False)
    session_id = 'acceptance'
    store = Store(root / 'projects.sqlite')
    runtime = RecordedRuntime(root, limit=2)
    result = {'id': case['id'], 'repeat': repeat, 'expected': case['expected'],
              'intent': 'FAIL', 'completion': 'not_executed', 'unsafe_executions': 0,
              'confirmation_origin': 'scripted_fixture_NOT_human_acceptance'}
    parameters = StdioServerParameters(command=sys.executable,
        args=['-B', '-m', 'week11.src.mcp_server', '--db', str(store.db_path), '--session', session_id],
        cwd=str(WEEK11_ROOT.parent))
    cli = None
    try:
        with (root / 'transcript.txt').open('w', encoding='utf-8') as transcript:
            with redirect_stdout(transcript):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        cli = ObservedCLI(session, store, session_id, runtime)
                        await cli.initialize()
                        if case.get('project', True) is not None:
                            await cli._command('/new acceptance ' + case['id'])
                        if case.get('baseline'):
                            result['baseline'] = await seed_baseline(cli, store, baseline)
                        cli.last_proposal_id = None
                        cli.events.clear()
                        with store.reading() as connection:
                            before_tasks = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
                        for turn, text in enumerate(case['turns']):
                            print('USER FIXTURE: ' + text, flush=True)
                            await cli._natural_language(text)
                            if case['id'] == 'missing_then_supply' and turn == 0:
                                result['first_turn_clarification'] = cli.last_proposal_id is None
                        with store.reading() as connection:
                            result['unsafe_executions'] = connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] - before_tasks
                        if result['unsafe_executions']:
                            raise ValueError('model started an execution before this test confirmation')
                        result['model_tool_events'] = deepcopy(cli.events)
                        if cli.last_proposal_id:
                            proposal = store.proposal(cli.last_proposal_id)
                            result['proposal'] = proposal
                            actual = {item['name']: item['value'] for item in proposal['spec']['normalized_parameters']}
                            expected = expected_parameters(case['id'])
                            intent_ok = set(actual) == set(expected) and all(math.isclose(actual[k], v, rel_tol=1e-9, abs_tol=1e-12) for k, v in expected.items())
                            if case['id'] in {'conflicting_voltage', 'unsupported_topology', 'no_project', 'evidence'}:
                                intent_ok = False
                            if case['id'] == 'missing_then_supply':
                                intent_ok = intent_ok and result.get('first_turn_clarification', False)
                            if not intent_ok:
                                raise ValueError('proposal differs from frozen expected intent/parameters')
                            result['intent'] = 'PASS'
                            result['execution'] = await execute_fixture(cli, store)
                            result['completion'] = 'PASS_scripted'
                            result['new_version_evidence'] = await cli._call('evidence_query', {'question': '请展示电容的模型原始声明。' if case['id'] != 'dual_public' else '请展示房间A热容的模型原始声明。'})
                        elif case['id'] == 'evidence':
                            answers = [event['result'] for event in cli.events if event['name'] == 'evidence_query' and event['result'].get('ok')]
                            if answers:
                                answer = answers[-1]['evidence']['answer']['evidence_answer']
                                if {item.get('layer') for item in answer.get('items', [])} >= {'sysml', 'modelica'}:
                                    result['intent'] = 'PASS'
                                    result['completion'] = 'PASS_readonly'
                        elif case['id'] in {'conflicting_voltage', 'unsupported_topology', 'no_project'}:
                            result['intent'] = 'PENDING_TEXT_REVIEW'
                            result['completion'] = 'safe_no_execution'
                        if result['intent'] == 'FAIL':
                            result['error'] = 'expected actionable proposal or evidence was not produced'
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    result['model_calls'] = runtime.calls
    if cli:
        result['all_tool_events'] = cli.events
    save(root / 'result.json', result)
    return result


async def run(root: Path, smoke: Path) -> int:
    root = root.resolve()
    root.relative_to(WEEK11_ROOT.resolve())
    smoke = smoke.resolve()
    smoke.relative_to(WEEK11_ROOT.resolve())
    root.mkdir(parents=True, exist_ok=False)
    protocol_path = WEEK11_ROOT / 'configs/online_acceptance.json'
    protocol = json.loads(protocol_path.read_text(encoding='utf-8'))
    first = json.loads((smoke / 'summary.json').read_text(encoding='utf-8'))
    baseline = Store(smoke / 'projects.sqlite').proposal(first['proposal']['proposal']['proposal_id'])['spec']
    snapshot = {str(path.relative_to(WEEK11_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (WEEK11_ROOT / 'src').glob('*.py')}
    snapshot['configs/online_acceptance.json'] = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    save(root / 'frozen-code.json', snapshot)
    summary = {'schema_version': 'v10-online-batch-1', 'planned_sessions':24,
               'confirmation_origin':'scripted_fixture_NOT_human_acceptance', 'results':[]}
    for repeat in range(1, protocol['repeats'] + 1):
        for case in protocol['cases']:
            item = await run_case(root / f'{repeat:02d}-{case["id"]}', case, repeat, baseline)
            summary['results'].append({k: item.get(k) for k in ('id','repeat','intent','completion','unsafe_executions','model_calls','error')})
            save(root / 'summary.json', summary)
            print(json.dumps(summary['results'][-1], ensure_ascii=False), flush=True)
            if item['unsafe_executions']:
                summary['stop_reason'] = 'unsafe_execution'
                save(root / 'summary.json', summary)
                return 2
            with runtime_connection() as connection:
                if connection.execute("SELECT 1 FROM meta WHERE key='frozen'").fetchone():
                    summary['stop_reason'] = 'runtime_frozen'
                    save(root / 'summary.json', summary)
                    return 2
    return 0


def runtime_connection():
    from contextlib import closing
    import sqlite3
    return closing(sqlite3.connect(WEEK11_ROOT / 'results/runtime-ledger.sqlite'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--smoke', type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(run(args.output, args.smoke))


if __name__ == '__main__':
    raise SystemExit(main())
