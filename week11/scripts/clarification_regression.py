"""One bounded regression on new values; preserve original acceptance failure."""
from __future__ import annotations
import asyncio
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from week11.scripts.cli_smoke import RecordedRuntime
from week11.scripts.online_acceptance import ObservedCLI, execute_fixture, save
from week11.src.store import Store, WEEK11_ROOT


async def main():
    root = WEEK11_ROOT / 'results/clarification-regression-20260915'
    root.mkdir(exist_ok=False)
    protocol = json.loads((WEEK11_ROOT / 'configs/clarification_regression.json').read_text(encoding='utf-8'))
    summary = {'scope':'targeted regression; original batch remains FAIL 20/24', 'results':[]}
    for case in protocol['cases']:
        path = root / case['id']
        path.mkdir()
        store = Store(path / 'projects.sqlite')
        runtime = RecordedRuntime(path, case['max_model_calls'])
        result = {'id':case['id'], 'confirmation_origin':'scripted_fixture_NOT_human_acceptance'}
        params = StdioServerParameters(command=sys.executable,args=['-B','-m','week11.src.mcp_server','--db',str(store.db_path),'--session','regression'],cwd=str(WEEK11_ROOT.parent))
        try:
            with (path/'transcript.txt').open('w',encoding='utf-8') as transcript:
                with redirect_stdout(transcript):
                    async with stdio_client(params) as (read,write):
                        async with ClientSession(read,write) as session:
                            cli = ObservedCLI(session,store,'regression',runtime)
                            await cli.initialize()
                            await cli._command('/new targeted '+case['id'])
                            for text in case['turns']:
                                print('USER FIXTURE: '+text,flush=True)
                                await cli._natural_language(text)
                            if case['id']=='missing_new_values':
                                if not cli.last_proposal_id:
                                    raise ValueError('supplement did not produce actionable proposal')
                                proposal = store.proposal(cli.last_proposal_id)
                                params_actual={p['name']:p['value'] for p in proposal['spec']['normalized_parameters']}
                                if any(params_actual[k]!=v for k,v in case['expected'].items()):
                                    raise ValueError('unexpected parameters')
                                result['execution']=await execute_fixture(cli,store)
                                result['status']='PASS'
                            else:
                                result['status']='FAIL' if cli.last_proposal_id else 'PENDING_TEXT_REVIEW'
                            result['events']=cli.events
        except Exception as exc:
            result.update(status='FAIL',error=f'{type(exc).__name__}: {exc}')
        result['model_calls']=runtime.calls
        save(path/'result.json',result)
        summary['results'].append({k:result.get(k) for k in ('id','status','model_calls','error')})
        save(root/'summary.json',summary)
        print(json.dumps(summary['results'][-1],ensure_ascii=False),flush=True)


if __name__=='__main__':
    asyncio.run(main())
