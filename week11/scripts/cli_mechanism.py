"""Scripted CLI confirmation mechanism test; never a claim of human acceptance."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from week11.scripts.cli_smoke import RecordedRuntime, Tee
from week11.scripts.audit_version import audit
from week11.src.cli import CLI
from week11.src.store import Store, WEEK11_ROOT


async def run(output: Path, smoke: Path, changes: bool) -> int:
    output = output.resolve()
    output.relative_to(WEEK11_ROOT.resolve())
    smoke = smoke.resolve()
    smoke.relative_to(WEEK11_ROOT.resolve())
    output.mkdir(parents=True, exist_ok=False)
    source_summary = json.loads((smoke / 'summary.json').read_text(encoding='utf-8'))
    original = Store(smoke / 'projects.sqlite').proposal(source_summary['proposal']['proposal']['proposal_id'])['spec']
    db = output / 'projects.sqlite'
    store = Store(db)
    runtime = RecordedRuntime(output, limit=2) if changes else None
    summary = {'schema_version':'v10-cli-mechanism-1', 'confirmation_origin':'scripted_fixture_NOT_human_acceptance',
               'real_mcp':True, 'real_simulation':True, 'steps':[]}
    parameters = StdioServerParameters(command=sys.executable,
        args=['-B','-m','week11.src.mcp_server','--db',str(db),'--session','mechanism'], cwd=str(WEEK11_ROOT.parent))
    try:
        with (output / 'transcript.txt').open('w', encoding='utf-8') as transcript:
            with redirect_stdout(Tee(transcript)):
                print('SCRIPTED CONFIRMATION MECHANISM TEST; NOT HUMAN ACCEPTANCE', flush=True)
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        cli = CLI(session, store, 'mechanism', runtime)
                        await cli.initialize()
                        await cli._command('/new V10 scripted RC mechanism')
                        replacements = {}
                        for source in original['sources']:
                            saved = store.record_source('mechanism', source['text'])
                            replacements[source['id']] = saved['source_id']
                            source['id'] = saved['source_id']
                        for item in original['parameters'] + original['derivations'] + [original['stimulus']]:
                            item['source_id'] = replacements[item['source_id']]
                        await cli._command('/proposal ' + json.dumps(original, ensure_ascii=False))
                        for index, modification in enumerate([None] + (['把输入电压改成 6 V，其余沿用当前版本。', '截止频率换成 2 kHz，保持电阻和阶跃输入不变，按原公式联动电容。'] if changes else [])):
                            if modification:
                                cli.last_proposal_id = None
                                print('USER FIXTURE: ' + modification, flush=True)
                                await cli._natural_language(modification)
                            proposal_id = cli.last_proposal_id
                            if not proposal_id:
                                raise ValueError('no actionable proposal from this step')
                            with patch('builtins.input', return_value='CONFIRM ' + proposal_id):
                                await cli._command('/confirm ' + proposal_id)
                            task = await cli._call('execution_status', {'task_id':cli.last_task_id})
                            summary['steps'].append({'proposal':store.proposal(proposal_id), 'task':task})
                            if task.get('task', {}).get('state') != 'succeeded':
                                raise ValueError('real worker did not publish a validated version')
                            project = store.current_project('mechanism')
                            version = store.version(project['project_id'], task['task']['version_id'])
                            checked = audit(WEEK11_ROOT / version['directory'], version['manifest_sha256'])
                            summary['steps'][-1]['audit'] = checked
                            if not all(checked[k] for k in ('technical','binding','evidence')):
                                raise ValueError('published artifact audit failed')
                            summary['steps'][-1]['evidence'] = await cli._call('evidence_query', {'question':'这个电容来自哪条需求？请展示两份模型的原始声明。'})
                        summary['versions'] = await cli._call('versions_list', {})
                        if changes:
                            summary['comparison'] = await cli._call('versions_compare', {'before_version_id':'v000001','after_version_id':'v000003'})
    except Exception as exc:
        summary['error'] = f'{type(exc).__name__}: {exc}'
    summary['model_calls'] = runtime.calls if runtime else 0
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'saved':str(output), 'steps':len(summary['steps']), 'error':summary.get('error')}, ensure_ascii=False), flush=True)
    return 1 if summary.get('error') else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--smoke', type=Path, required=True)
    parser.add_argument('--changes', action='store_true', help='at most 2 extra online smoke requests')
    args = parser.parse_args()
    return asyncio.run(run(args.output, args.smoke, args.changes))


if __name__ == '__main__':
    raise SystemExit(main())
