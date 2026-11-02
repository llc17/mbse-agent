"""Isolated original-input catalog and actual Store/TaskFlow integration.

All generated model output, budget approval and terminal execution are TEST_ONLY
in a private fixture directory. No HTTP/MCP/compiler/simulation is dispatched.
"""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from week13.src import case_paths,case_projects,case_requests,case_taskflow
from week13.src.case_backend import module
from week13.src.case_budget import CaseBudget
from week13.src.case_runtime import CaseRuntime
from week13.tests.test_case_agent_requests import finish_fixture,NoLaunch

EVIDENCE=case_paths.ROOT/'results/case-management-20261020-01'


def sysml(spec):
    from week13.src.case_scope import modelica_parameters
    values,expressions,kinds=modelica_parameters(spec['artifact']['modelica_source'])
    attrs=''.join('attribute '+name+' : '+('Integer' if kinds[name]=='Integer' else 'Real')+' = '+expressions[name]+';' for name in values)
    reqs=''
    for i,requirement in enumerate(spec['requirements'],1):
        traces=dict(requirement,model_name=spec['artifact']['model_name'])
        reqs+='requirement req_'+str(i)+' {'+''.join('attribute '+key+' : String = '+json.dumps(value,ensure_ascii=False)+';' for key,value in traces.items())+'}'
    return 'package IsolatedCase { part def System {'+attrs+'} part system : System;'+reqs+'}'


class CaseProjects(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='backend-fixture-',dir=EVIDENCE);self.root=Path(self.temp.name)
        (self.root/'configs').mkdir();(self.root/'configs/case_runtime.json').write_bytes((case_paths.ROOT/'configs/case_runtime.json').read_bytes())
        self.stack=ExitStack()
        for obj,key,value in [(case_projects,'ROOT',self.root),(case_projects,'LEGACY_ROOT',self.root/'no-legacy'),(case_requests,'ROOT',self.root),(case_requests,'LEGACY_PROJECT_ROOT',self.root/'no-legacy'),(case_requests,'REQUESTS_ROOT',self.root/'no-legacy/requests'),(case_taskflow,'PROJECT_ROOT',self.root/'no-legacy'),(case_taskflow,'PROJECT_DB',self.root/'no-legacy/projects.sqlite')]:self.stack.enter_context(patch.object(obj,key,value))
        self.inputs=self.root/'inputs';self.inputs.mkdir()
        self.original=case_projects.REFERENCE_MODEL.read_bytes();(self.inputs/'original.mo').write_bytes(self.original)
        self.calls=[];self.outcome_status='PASS'
    def tearDown(self):
        self.stack.close();self.temp.cleanup()
    def imported(self,name='TEST_ONLY A',**kwargs):
        result=case_projects.import_case(self.inputs,name,success_statement='TEST_ONLY 用户声明原始85kg完整20上20下成功；不是本次运行。',**kwargs)
        self.assertTrue(result['created'],result);return result['case']
    def bootstrap(self,case,rid,version='v000000',text='TEST_ONLY 在原重心增加5kg理想点载，总质量90kg，I及原结构控制任务不变。'):
        return case_taskflow.bootstrap(rid,case['project_id'],version,text)
    def authorize(self,rid,budget_id=None,reuse=False):
        caps=dict(http_requests=12,product_cny=100,total_tokens=12000000,total_output_tokens=12*393216,compile=4,simulation=4,candidates=3,software_repair_rounds=1,parameter_revision_rounds=0,observation_extensions=0)
        return case_requests.authorize_budget(rid,budget_id or rid+'_budget','CONFIRM',reuse=reuse,test_only=True,**({} if reuse else dict(caps=caps,hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())))
    def runtime(self,ctx):
        return CaseRuntime(Path(ctx['root'])/'runtime.json',task_context=ctx,transport=lambda payload,*args:self.transport(ctx,payload))
    def transport(self,ctx,payload):
        data=json.loads(payload['messages'][1]['content']);tool=(payload.get('tools') or [{}])[0].get('function',{}).get('name');self.calls.append((ctx['project_id'],tool))
        if tool=='case_request_plan':
            p=dict(m=90,I_pitch=20,T_main_max=60,T_nose_max=25)
            requirement=dict(text=ctx['raw_request'],source_id=ctx['sources'][0]['id'],source_text=ctx['raw_request'])
            packet=dict(intent='CHANGE',support='SUPPORTED',missing=[],unsupported=[],requirements=[requirement],design=dict(summary='TEST_ONLY 产品有限点载方案',assumptions=[dict(text='原重心理想点载，惯量不变',origin='model')],parameters=[dict(name=k,value=v,unit='kg' if k=='m' else '1',origin='model',basis='TEST_ONLY injected product interpretation') for k,v in p.items()]),proposed_parameters=p,defaults=['继承DASSL和原任务条件；确认后补齐原始基准SysML及候选双源码，各自保持来源。'],unknowns=['实物未验证'],rationale='TEST_ONLY',scope=dict(change_kind='center_of_mass_point_load',mass_location='original_center_of_mass',inertia_assumption='ideal_point_mass',frozen='geometry_contact_control_terrain_task'))
            args=dict(packet=packet)
        elif tool=='proposal_prepare':
            base=ctx['baseline_snapshot']['frozen_spec'];spec=deepcopy(base)
            spec.update(baseline_ref=ctx['baseline_ref'],sources=ctx['sources'],requirements=ctx['plan']['requirements'],design=ctx['plan']['design'])
            spec['artifact']['modelica_source']=re.sub(r'(\bparameter\s+Real\s+m\s*=\s*)[^;]+;',lambda m:m[1]+'90;',spec['artifact']['modelica_source'])
            spec['artifact']['sysml_source']=sysml(spec);args=dict(spec=spec)
            if data.get('original_baseline_completion'):args['baseline_sysml_source']=sysml(base)
        else:
            return dict(id='TEST_ONLY_reply',model='deepseek-flash',choices=[dict(message=dict(content='TEST_ONLY 结果绑定；不是实际物理运行。',reasoning_content='fixture'),finish_reason='stop')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
        return dict(id='TEST_ONLY_reply',model='deepseek-flash',choices=[dict(message=dict(content='',reasoning_content='fixture',tool_calls=[dict(id='TEST_ONLY_call',type='function',function=dict(name=tool,arguments=json.dumps(args,ensure_ascii=False)))]),finish_reason='tool_calls')],usage=dict(prompt_tokens=20,completion_tokens=10,total_tokens=30))
    async def rpc(self,flow,name,args):
        svc=flow.open();svc.worker_manager=NoLaunch();result=getattr(svc,name)(**args)
        if name=='execution_start' and result['task']['state']=='queued':
            # The inherited synthetic helper writes text using Windows default
            # newline conversion. Freeze exactly the product bytes instead.
            original_write=Path.write_text
            def exact_write(path,text,*args,**kwargs):
                if path.name=='model_res.csv':
                    text='time,'+','.join(case_projects.OUTPUT_NAMES)+'\n0,'+','.join('0' for _ in case_projects.OUTPUT_NAMES)+'\n120,'+','.join('1' for _ in case_projects.OUTPUT_NAMES)+'\n'
                kwargs['newline']='';return original_write(path,text,*args,**kwargs)
            with patch.object(Path,'write_text',exact_write):finish_fixture(svc,result['task'],flow.ctx['project_root'],self.outcome_status)
        return result
    def flow(self,rid):return case_taskflow.TaskFlow.load(rid,runtime_factory=self.runtime,rpc=self.rpc)
    def completed(self,case,rid):
        original=self.bootstrap(case,rid);self.authorize(rid);flow=self.flow(rid)
        planned=asyncio.run(flow.plan());flow.confirm(planned['displayed_digest'],'CONFIRM')
        result=asyncio.run(flow.run());return original,result
    def test_original_no_sysml_full_flow_and_cold_selector(self):
        case=self.imported();discovered=case_projects.discover_case(case['case_id'])
        self.assertEqual(discovered['default_baseline']['version_id'],'v000000');self.assertTrue(discovered['default_baseline']['selectable'])
        original,result=self.completed(case,'TEST_ONLY_first');ctx=case_requests.load_request('TEST_ONLY_first')
        self.assertEqual(result['state']['latest_request_execution']['version_id'],'v000001')
        self.assertEqual(result['generation']['delta']['parameters']['m'],90)
        self.assertEqual(case_taskflow.modelica_parameters(original['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'])[0]['m'],85)
        self.assertNotIn('sysml_source',original['baseline_snapshot']['frozen_spec']['artifact'])
        self.assertEqual(original['baseline_ref'],ctx['baseline_ref']);self.assertEqual(original['request_digest'],ctx['request_digest'])
        sidecar=json.loads((Path(ctx['root'])/'baseline-sysml-completion.json').read_text())
        self.assertEqual(sidecar['completed_baseline_spec']['requirements'],original['baseline_snapshot']['frozen_spec']['requirements'])
        self.assertNotEqual(sidecar['completed_baseline_spec']['requirements'],ctx['plan']['requirements'])
        self.assertTrue(case_taskflow.baseline_sysml_for_context(ctx).strip())
        code="import json,sys;from pathlib import Path;from week13.src import case_requests as r,case_projects as c;r.ROOT=Path(sys.argv[1]);r.LEGACY_PROJECT_ROOT=r.ROOT/'no-legacy';r.REQUESTS_ROOT=r.LEGACY_PROJECT_ROOT/'requests';c.ROOT=r.ROOT;c.LEGACY_ROOT=r.LEGACY_PROJECT_ROOT;from week13.src import case_paths as p;from week13.src.case_session import open_service;from week13.src.case_worker import WEEK12_ROOT;from week13.src.case_mcp import baseline_warmup_source;s=open_service();print(json.dumps(dict(project_root=str(p.PROJECT_ROOT),db=str(p.PROJECT_DB),ledger=str(p.BUDGET_DB),worker_root=str(WEEK12_ROOT),project_id=s._current_project()['project_id'],sysml=bool(baseline_warmup_source(p.TASK_CONTEXT,p.PROJECT_ROOT)))))"
        child=subprocess.run([sys.executable,'-X','utf8','-B','-c',code,str(self.root)],cwd=case_paths.REPO,env=dict(os.environ,MBSE_V12_REQUEST_ID=ctx['request_id'],PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True,encoding='utf8',timeout=60)
        self.assertEqual(child.returncode,0,child.stderr);selected=json.loads(child.stdout)
        self.assertEqual(selected['project_root'],case['project_root']);self.assertEqual(selected['worker_root'],case['project_root']);self.assertEqual(selected['db'],case['db_path']);self.assertEqual(selected['ledger'],ctx['runtime_authority']['ledger_path']);self.assertTrue(selected['sysml'])
        before=self.flow(ctx['request_id']).state();count=CaseBudget(task_context=ctx).usage()['requests'];reopened=asyncio.run(self.flow(ctx['request_id']).ask('TEST_ONLY 退出后重开，请解释绑定版本。',reopen=True));after=self.flow(ctx['request_id']).state()
        self.assertTrue(reopened['read_only_status_unchanged']);self.assertEqual({k:v for k,v in before.items() if k!='http_state'},{k:v for k,v in after.items() if k!='http_state'});self.assertEqual(CaseBudget(task_context=ctx).usage()['requests'],count+1)
        completion=Path(ctx['root'])/'baseline-sysml-completion.json';changed=deepcopy(sidecar);changed['request_digest']='0'*64;completion.write_text(json.dumps(changed),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'binding drift'):asyncio.run(flow_run(self.flow(ctx['request_id'])))
    def test_two_projects_replay_old_branch_and_version_comparison(self):
        a=self.imported();_,first=self.completed(a,'TEST_ONLY_A')
        replay=case_projects.replay_case(a['case_id'],name='TEST_ONLY B');b=replay['case']
        self.assertEqual(b['origin_case_id'],a['case_id']);self.assertEqual(b['input_sha256'],a['input_sha256']);self.assertNotEqual(b['project_id'],a['project_id'])
        self.assertFalse((Path(b['project_root'])/'baseline-generated.sysml').exists());self.assertEqual(list((Path(b['project_root'])/'requests').glob('*')),[])
        ctx=self.bootstrap(b,'TEST_ONLY_B');self.assertIsNone(ctx['runtime_authority']);self.assertNotIn('sysml_source',ctx['baseline_snapshot']['frozen_spec']['artifact'])
        self.authorize('TEST_ONLY_B')
        with self.assertRaises(PermissionError):self.authorize('TEST_ONLY_B',budget_id='TEST_ONLY_A_budget',reuse=True)
        plan=asyncio.run(self.flow('TEST_ONLY_B').plan());self.flow('TEST_ONLY_B').confirm(plan['displayed_digest'],'CONFIRM');second=asyncio.run(self.flow('TEST_ONLY_B').run())
        self.assertEqual(second['state']['latest_request_execution']['version_id'],'v000001')
        old=self.bootstrap(a,'TEST_ONLY_A_branch',version='v000000');self.assertEqual(old['baseline_ref']['version_id'],'v000000');self.assertEqual(case_taskflow.modelica_parameters(old['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'])[0]['m'],85)
        self.authorize('TEST_ONLY_A_branch',budget_id='TEST_ONLY_A_budget',reuse=True);plan=asyncio.run(self.flow('TEST_ONLY_A_branch').plan());self.flow('TEST_ONLY_A_branch').confirm(plan['displayed_digest'],'CONFIRM')
        branch=asyncio.run(self.flow('TEST_ONLY_A_branch').run());self.assertEqual(branch['state']['latest_request_execution']['version_id'],'v000002')
        comparison=case_projects.compare_versions(a['case_id'],'v000000','v000002')
        self.assertEqual(comparison['parameter_changes']['m'],dict(before=85,after=90));self.assertIsNone(comparison['left']['completion_time']);self.assertFalse(comparison['directly_comparable']);self.assertFalse(comparison['automatic_performance_ranking'])
        self.assertEqual([x['request_id'] for x in case_projects.discover_case(b['case_id'])['requests']],['TEST_ONLY_B'])
        self.assertEqual(case_projects.discover_case(a['case_id'])['default_baseline']['version_id'],'v000002')
        older=self.bootstrap(a,'TEST_ONLY_A_v1_branch',version='v000001');self.assertEqual(older['baseline_ref']['version_id'],'v000001');self.assertNotEqual(older['baseline_ref'],case_requests.load_request('TEST_ONLY_A_branch')['baseline_ref'])
        self.assertEqual(CaseBudget(task_context=case_requests.load_request('TEST_ONLY_B')).usage()['counters']['candidate'],1)
    def test_multiple_models_duplicate_security_docx_and_unsupported_management(self):
        (self.inputs/'up.mo').write_text('model Up parameter Real m=sin(2); equation end Up;',encoding='utf8')
        (self.inputs/'.env').write_text('NOT_READ',encoding='utf8');(self.inputs/'~$locked.docx').write_bytes(b'NOT_A_DOCX')
        with ZipFile(self.inputs/'notes.docx','w') as archive:
            archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>TEST_ONLY 说明</w:t></w:r></w:p></w:document>');archive.writestr('word/media/image1.png',b'fixture')
        (self.inputs/'hint.sysml').write_text('package Unverified {}',encoding='utf8')
        viewed=case_projects.inspect_case(self.inputs);self.assertIsNone(viewed['selected_model']);self.assertEqual(len(viewed['model_candidates']),2)
        self.assertFalse(case_projects.import_case(self.inputs,'TEST_ONLY unselected')['created'])
        doc=next(x for x in viewed['documents'] if x['path']=='notes.docx');self.assertIn('TEST_ONLY 说明',doc['text']);self.assertFalse(doc['ocr_performed']);self.assertEqual(doc['embedded_images'],'NOT_AUTOMATICALLY_INTERPRETED')
        self.assertTrue(any(x.get('status')=='UNVERIFIED_SOURCE_PAIR' for x in viewed['documents']))
        created=case_projects.import_case(self.inputs,'TEST_ONLY unsupported',model='up.mo',success_statement='TEST_ONLY 用户声明')['case']
        data=case_projects.discover_case(created['case_id']);self.assertIsNone(data['default_baseline']);self.assertEqual(data['support']['status'],'UNSUPPORTED')
        comparison=case_projects.compare_versions(created['case_id'],'v000000','v000000');self.assertEqual(comparison['left']['parameters'],{});self.assertFalse(comparison['directly_comparable'])
        duplicate=case_projects.import_case(self.inputs,'TEST_ONLY duplicate',model='up.mo');self.assertFalse(duplicate['created']);self.assertTrue(duplicate['duplicates'])
        separate=case_projects.import_case(self.inputs,'TEST_ONLY separate',model='up.mo',duplicate_mode='independent');self.assertTrue(separate['created']);self.assertEqual(separate['case']['origin_case_id'],created['case_id'])
        self.assertFalse((Path(created['project_root'])/'original/.env').exists())
        with self.assertRaisesRegex(ValueError,'forbidden'):case_projects.inspect_case('D:/v8_holdout_private')
        with self.assertRaisesRegex(ValueError,'too broad'):case_projects.inspect_case(case_paths.REPO)
        ctx=self.bootstrap(created,'TEST_ONLY_UNSUPPORTED');self.authorize(ctx['request_id'])
        with self.assertRaisesRegex(ValueError,'unsupported'):asyncio.run(self.flow(ctx['request_id']).plan())
    def test_source_drift_rejected_and_historical_statement_optional(self):
        case=case_projects.import_case(self.inputs,'TEST_ONLY missing success')['case'];self.assertTrue(case_projects.discover_case(case['case_id'])['default_baseline']['selectable'])
        ctx=self.bootstrap(case,'TEST_ONLY_missing');self.authorize(ctx['request_id'])
        planned=asyncio.run(self.flow(ctx['request_id']).plan());self.assertTrue(planned['validation']['executable'])
        original=Path(case['project_root'])/'original/original.mo';original.write_bytes(self.original+b'\n// drift')
        with self.assertRaisesRegex(ValueError,'source drift'):case_requests.load_request(ctx['request_id'])
    def test_snapshot_ancestor_link_is_rejected_before_hashing_target(self):
        case=self.imported();original=Path(case['project_root'])/'original';real_file=original/'original.mo'
        # Simulate Windows junction detection portably without needing symlink
        # privilege. The target must be rejected before its bytes are hashed.
        original_hash=case_projects._sha;is_symlink=Path.is_symlink;hashed=[]
        def marked(path):return Path(path)==original or is_symlink(path)
        def tracked(path):
            hashed.append(Path(path));return original_hash(path)
        with patch.object(Path,'is_symlink',marked),patch.object(case_projects,'_sha',tracked):
            with self.assertRaisesRegex(ValueError,'symbolic links/junctions'):case_projects.verify_case(case)
        self.assertNotIn(real_file,hashed)


async def flow_run(flow):return await flow.run()


if __name__=='__main__':unittest.main()
