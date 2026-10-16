"""Current CLI -> real stdio/Service/SysML -> TEST_ONLY Worker, zero HTTP/OMC."""
import asyncio, contextlib, io, json, sqlite3, sys, unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from week12.tests import test_artifact_continuous_cli as base
from week12.src.car_domain.artifact_continuous import FLOW_PROMPT
from week12.src.car_domain.artifact_cli import ArtifactCLI,async_main
from week12.src.car_domain.model_artifact import save

OUT=Path(__file__).resolve().parents[1]/'results/s5-continuous-chain-hardening-20261014-01'
FACTS=[]
TEXT='TEST_ONLY current chain: 质量1kg；1秒末值 signal >= 0；另一个判据是 signal >= 1。非车辆证据。'

class Provider(base.ControlledProvider):
    def __init__(self,root,*,kind='repair',threshold=0):
        super().__init__(root,cap=4);self.kind=kind;self.threshold=threshold
    def complete(self,messages,tools,rid):
        if rid in self.cache:return deepcopy(self.cache[rid])
        response=super().complete(messages,tools,rid);response['finish_reason']='stop'
        if messages[0]['content'].startswith(FLOW_PROMPT):
            if self.kind in ('bad','length') or (self.kind=='repair' and not rid.endswith('_format1')):
                response['content']='{"schema":'
            else:
                value=json.loads(response['content']);value['commitment']['checks'][0]['threshold']=self.threshold
                response['content']=json.dumps(value,ensure_ascii=False)
            if self.kind=='length':response['finish_reason']='length'
        else:
            value=json.loads(response['tool_calls'][0]['function']['arguments']);spec=value['spec'];spec['checks'][0]['threshold']=self.threshold
            if self.kind=='sysml':spec['artifact']['sysml_source']='package Empty {}'
            if self.kind=='drift':spec['artifact']['modelica_source']=spec['artifact']['modelica_source'].replace('mass=1','mass=2')
            response['tool_calls'][0]['function']['arguments']=json.dumps(value,ensure_ascii=False)
        self.cache[rid]=deepcopy(response);return response

class Checks(unittest.IsolatedAsyncioTestCase):
    def setup(self,kind='repair',threshold=0,tool_failure=False):
        old=base.OUT;base.OUT=OUT
        try:root,args,_=base.Checks().setup()
        finally:base.OUT=old
        if tool_failure:
            value=json.loads((root/'context.json').read_text(encoding='utf8'));value['test_tool_failure']=True;save(root/'context.json',value)
        return root,args,Provider(root,kind=kind,threshold=threshold)
    async def run_case(self,root,args,provider,case):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):await async_main(args,runtime=provider,case=case)
        (root/'console.txt').write_text(output.getvalue(),encoding='utf8')
        return output.getvalue()
    def count(self,root,table):
        with contextlib.closing(sqlite3.connect(root/'projects.sqlite')) as db:return db.execute('SELECT count(*) FROM '+table).fetchone()[0]
    def result(self,root):
        with contextlib.closing(sqlite3.connect(root/'projects.sqlite')) as db:return json.loads(db.execute('SELECT result_json FROM tasks').fetchone()[0])
    async def test_format_repair_complete_dual_delivery_and_repeat(self):
        root,args,provider=self.setup()
        async def case(cli):
            designed=await cli.handle_line(TEXT)
            self.assertEqual(designed['format_corrections'],1);self.assertEqual(self.count(root,'tasks'),0)
            self.assertEqual(cli._flow_current()['request_id'],provider.calls[-1])
            await cli.handle_line('确认');await cli.handle_line('确认')
        output=await self.run_case(root,args,provider,case)
        result=self.result(root);self.assertEqual(result['task_status'],'PASS');self.assertEqual(result['toolchain_invocations'],0)
        self.assertEqual(result['sysml_status'],'PASS');self.assertEqual(result['consistency_status'],'PASS')
        self.assertEqual(len(provider.calls),3);self.assertEqual(self.count(root,'tasks'),1)
        manifest=json.loads(Path(result['outcome_manifest_path']).read_text(encoding='utf8'))
        for name in ('frozen-model.mo','frozen-model.sysml','tool/model_res.csv'):self.assertIn(name,manifest['files'])
        for text in ('自动纠正一次','SysML源码','Modelica源码','CSV'):self.assertIn(text,output)
        FACTS.append(dict(case='schema_repair_confirm_dual_sources_stdio_worker_results_repeat',root=str(root),controlled_calls=3,tasks=1,toolchain_invocations=0))
    async def test_observation_failure_is_distinct_from_tool_failure(self):
        for tool_failure in (False,True):
            root,args,provider=self.setup(kind='normal',threshold=1,tool_failure=tool_failure)
            async def case(cli):await cli.handle_line(TEXT);await cli.handle_line('确认')
            await self.run_case(root,args,provider,case);result=self.result(root)
            self.assertEqual(result['task_status'],'UNKNOWN' if tool_failure else 'FAIL')
            self.assertEqual(result['tool_status'],'TOOL_FAIL' if tool_failure else 'PASS')
            self.assertEqual(result['toolchain_invocations'],0)
            FACTS.append(dict(case='tool_failure' if tool_failure else 'observation_failure',root=str(root),tool=result['tool_status'],task=result['task_status'],controlled_calls=len(provider.calls)))
    async def test_invalid_sysml_literal_and_source_drift_never_execute(self):
        for kind in ('sysml','drift','source'):
            root,args,provider=self.setup(kind=kind)
            async def case(cli):
                await cli.handle_line(TEXT)
                if kind=='source':cli.store.record_source(cli.session_id,'TEST_ONLY changed source')
                with self.assertRaises(ValueError):await cli.handle_line('确认')
            await self.run_case(root,args,provider,case);self.assertEqual(self.count(root,'tasks'),0)
            FACTS.append(dict(case=kind+'_rejected',root=str(root),tasks=0,controlled_calls=len(provider.calls)))
    async def test_bad_correction_or_length_does_not_loop(self):
        for kind,expected in (('bad',2),('length',1)):
            root,args,provider=self.setup(kind=kind)
            async def case(cli):
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError,'有限纠正'):await cli.handle_line(TEXT)
                with self.assertRaisesRegex(ValueError,'可确认'):await cli.handle_line('确认')
            await self.run_case(root,args,provider,case)
            self.assertEqual(len(provider.calls),expected);self.assertEqual(self.count(root,'tasks'),0)
            FACTS.append(dict(case=kind+'_bounded_stop',root=str(root),controlled_calls=expected,tasks=0))
    def test_receipt_reordering_keeps_bytes_but_changes_are_rejected(self):
        root,args,provider=self.setup(kind='normal');cli=ArtifactCLI.__new__(ArtifactCLI);cli.runtime=provider
        first=dict(messages=[dict(role='system',content='TEST_ONLY exact source')],tools=[])
        cli._journal('test_receipt','request',first)
        path=next((root/'cli-turns').glob('*-request.json'));original=path.read_bytes()
        cli._journal('test_receipt','request',json.loads(json.dumps(first,sort_keys=True)))
        self.assertEqual(path.read_bytes(),original)
        changed=deepcopy(first);changed['messages'][0]['content']+=' changed'
        with self.assertRaisesRegex(ValueError,'immutable'):cli._journal('test_receipt','request',changed)

if __name__=='__main__':
    import time
    OUT.mkdir(parents=True,exist_ok=True);start=time.monotonic();log=io.StringIO()
    selected=sys.argv[1:]
    suite=unittest.TestSuite(Checks(name) for name in selected) if selected else unittest.defaultTestLoader.loadTestsFromTestCase(Checks)
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    suffix='-targeted' if selected else ''
    (OUT/('checks-console'+suffix+'.txt')).write_text(log.getvalue(),encoding='utf8')
    save(OUT/('verification'+suffix+'.json'),dict(passed=result.wasSuccessful(),tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),elapsed_s=time.monotonic()-start,product_http=0,omc=0,transport='REAL_STDIO_MCP',sysml='REAL_INSTALLED_PARSER',worker='TEST_ONLY_SYNTHETIC_CSV',facts=FACTS,selected=selected))
    print(log.getvalue());raise SystemExit(0 if result.wasSuccessful() else 1)
