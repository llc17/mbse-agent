"""Direct finite parameter-tool checks; no HTTP, MCP, compiler or simulation."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from week13.src import case_parameter_edit as edit, case_taskflow as flow
from week13.src.case_asym2_family import PLAN_PARAMETERS, _replace, modelica_parameters
from week13.src.case_paths import ROOT, read_json, sha_value
from week13.src.case_scope import MASS_SCOPE_PACKET, STAIR_SCOPE_PACKET, check_request_delta
from week13.src.inherited.model_artifact import validate_artifact

SNAPSHOT = ROOT/'runs/case-projects/case_380e7315172241efa971/requests/console_20261101T150324_6b18efafeb/baseline-snapshot.json'


def context(mass=None, height=None, depth=None, snapshot=None):
    snapshot = deepcopy(snapshot or read_json(SNAPSHOT)); base = snapshot['frozen_spec']
    old = modelica_parameters(base['artifact']['modelica_source'])[0]
    values = {name: old[name] for name in PLAN_PARAMETERS}
    values.update({k:v for k,v in dict(m=mass,stepH=height,stepD=depth).items() if v is not None})
    source = dict(id='src_parameter_fixture', text='TEST_ONLY 保留"引号"和中文\n新行的已确认要求。')
    requirement = dict(text=source['text'], source_id=source['id'], source_text=source['text'])
    plan = dict(intent='CHANGE', support='SUPPORTED', missing=[], unsupported=[],
        requirements=[requirement], design=dict(summary='TEST_ONLY 已确认产品方案', assumptions=[],
        parameters=[dict(name=k,value=v,unit='1',origin='model',basis='TEST_ONLY') for k,v in values.items()]),
        proposed_parameters=values, defaults=[], unknowns=[], rationale='TEST_ONLY',
        scope=deepcopy(STAIR_SCOPE_PACKET if height is not None or depth is not None else MASS_SCOPE_PACKET))
    reference={key:snapshot[key] for key in ('project_id','version_id','task_id','spec_digest','source_sha256','manifest_sha256')}
    return dict(request_id='fixture_parameter_tool',request_digest='a'*64,displayed_digest='b'*64,
        project_id=snapshot['project_id'],baseline_ref=reference,baseline_snapshot=snapshot,
        sources=[source],plan=plan)


class ParameterToolTests(unittest.TestCase):
    def test_three_independent_edits_preserve_source_and_full_dual_contract(self):
        original = read_json(SNAPSHOT)
        for updates, parameter in [({'mass':90.0},'m'),({'height':.075},'stepH'),({'height':.085},'stepH')]:
            with self.subTest(updates=updates):
                ctx=context(**updates); before=deepcopy(ctx)
                spec,audit=edit.apply_confirmed_plan(ctx)
                checked=validate_artifact(spec,require_sysml=True)
                self.assertTrue(checked['ready'],checked['issues'])
                base=ctx['baseline_snapshot']['frozen_spec']
                self.assertEqual(spec['artifact']['modelica_source'], _replace(base['artifact']['modelica_source'],parameter,ctx['plan']['proposed_parameters'][parameter]))
                self.assertEqual(spec['requirements'],ctx['plan']['requirements'])
                self.assertEqual(spec['design'],ctx['plan']['design']);self.assertEqual(spec['sources'],ctx['sources'])
                for key in ('settings','checks','outputs','libraries','trajectory_contract'):
                    self.assertEqual(spec.get(key),base.get(key))
                params,expressions,_=modelica_parameters(spec['artifact']['modelica_source'])
                self.assertEqual(expressions['H_peak'],'(n_steps-nEndSteps)*stepH+h19+h20')
                self.assertEqual(params['wheel_J'],[.02205,.12852225,.12852225])
                self.assertEqual([params[k] for k in ('h19','h20','d19','d20','T_main_max','T_nose_max','I_pitch')],[.0775,.065,.22,.08,40,17,20])
                self.assertEqual(audit['origin'],edit.ORIGIN);self.assertFalse(audit['product_generated_source'])
                self.assertEqual(audit['selected_baseline_mass'],85);self.assertEqual(audit['added_point_load'],5 if parameter=='m' else 0)
                self.assertEqual(ctx,before)
        self.assertEqual(read_json(SNAPSHOT),original)

    def test_selected_baseline_mass_is_lower_bound(self):
        ctx=context(mass=95);spec,_=edit.apply_confirmed_plan(ctx)
        snap=deepcopy(ctx['baseline_snapshot']);snap['frozen_spec']=spec
        checked=validate_artifact(spec,require_sysml=True)
        snap.update(spec_digest=checked['spec_digest'],source_sha256=checked['source_sha256'])
        with self.assertRaisesRegex(ValueError,'added ideal point mass'):
            edit.apply_confirmed_plan(context(mass=90,snapshot=snap))
        candidate,audit=edit.apply_confirmed_plan(context(mass=100,snapshot=snap))
        self.assertEqual(audit['selected_baseline_mass'],95);self.assertEqual(audit['added_point_load'],5)
        self.assertEqual(modelica_parameters(candidate['artifact']['modelica_source'])[0]['m'],100)

    def test_mixed_mass_height_and_frozen_design_rejected(self):
        for updates,changed in [({'mass':90,'height':.075},None),({'mass':90},'I_pitch'),({'height':.075},'T_main_max')]:
            ctx=context(**updates)
            if changed:
                ctx['plan']['proposed_parameters'][changed]+=1
                next(row for row in ctx['plan']['design']['parameters'] if row['name']==changed)['value']+=1
            with self.subTest(updates=updates,changed=changed),self.assertRaises(ValueError):edit.apply_confirmed_plan(ctx)

    def test_depth_and_missing_or_unverified_baseline_keep_old_generation_path(self):
        self.assertIsNone(edit.edit_mode(context(depth=.27)))
        for key,value in [('recorded_conclusions_verified',False),('evidence_status','NOT_RECHECKED')]:
            ctx=context(mass=90);ctx['baseline_snapshot'][key]=value
            self.assertIsNone(edit.edit_mode(ctx))
        ctx=context(mass=90);ctx['baseline_snapshot']['frozen_spec']['artifact'].pop('sysml_source')
        self.assertIsNone(edit.edit_mode(ctx))

    def test_baseline_parameter_and_sysml_tampering_rejected(self):
        for name in ('h19','I_pitch','T_main_max'):
            ctx=context(mass=90);artifact=ctx['baseline_snapshot']['frozen_spec']['artifact']
            artifact['modelica_source']=_replace(artifact['modelica_source'],name,123)
            with self.subTest(name=name),self.assertRaises(ValueError):edit.apply_confirmed_plan(ctx)
        ctx=context(mass=90);artifact=ctx['baseline_snapshot']['frozen_spec']['artifact']
        artifact['sysml_source']=artifact['sysml_source'].replace('attribute m : Real = 85.0','attribute m : Real = 86.0')
        with self.assertRaisesRegex(ValueError,'baseline dual-source'):edit.apply_confirmed_plan(ctx)

    def test_generate_entry_consumes_candidate_and_uses_existing_validation_without_http(self):
        ctx=context(mass=90)
        with tempfile.TemporaryDirectory(prefix='parameter-entry-',dir=ROOT/'results/two-simple-capabilities-20261102-01/A') as temporary:
            ctx['root']=temporary;ctx['runtime_authority']=dict(ledger_path=str(Path(temporary)/'fixture-budget.sqlite'))
            worker=flow.TaskFlow(ctx);worker.refresh=lambda:None
            svc=SimpleNamespace(normalize_candidate=lambda spec:(spec,None),_current_project=lambda:dict(project_id=ctx['project_id']))
            worker.open=lambda **kwargs:svc
            worker.call=AsyncMock(side_effect=AssertionError('parameter tool must not issue generation HTTP'))
            worker._prepare_generation=AsyncMock(side_effect=lambda spec,delta,checked,completion:dict(spec=spec,delta=delta,checked=checked))
            budget=SimpleNamespace(consume=unittest.mock.Mock())
            with patch('week13.src.case_requests.require_execution'),patch('week13.src.case_local_proposal_recovery.preflight'),patch.object(flow,'validate_import_plan'),patch('week13.src.case_budget.CaseBudget',return_value=budget),patch('week13.src.inherited.artifact_baseline.selection',return_value=ctx['baseline_ref']),patch('week13.src.inherited.artifact_baseline.resolve',return_value=ctx['baseline_snapshot']),patch.object(flow,'check_delta',side_effect=lambda service,spec,task_context:check_request_delta(service,spec,task_context)):
                actual=asyncio.run(worker.generate())
            self.assertEqual(actual['delta']['status'],'PASS_REQUEST_FAMILY_DELTA')
            self.assertTrue(actual['checked']['ready']);worker.call.assert_not_called()
            budget.consume.assert_called_once();self.assertEqual(budget.consume.call_args.args[0],'candidate')
            audit=read_json(Path(temporary)/'parameter-edit-provenance.json')
            self.assertEqual(audit['candidate_spec_digest'],actual['checked']['spec_digest'])
            self.assertEqual(worker._parameter_edit_provenance(actual['spec']),audit)
            changed=deepcopy(actual['spec']);changed['artifact']['modelica_source']+=' '
            with self.assertRaisesRegex(ValueError,'provenance'):worker._parameter_edit_provenance(changed)

    def test_existing_non_asym_generation_uses_product_and_mcp_readback(self):
        from week13.src import case_requests
        from week13.src.case_budget import CaseBudget
        from week13.tests.test_case_agent_requests import Harness
        from week13.tests.test_case_flat_plan_protocol import flat
        saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        try:
            with tempfile.TemporaryDirectory(prefix='legacy-entry-',dir=ROOT/'results/two-simple-capabilities-20261102-01/A') as temporary:
                harness=Harness(Path(temporary)/'fixture');original=harness.transport
                def current_wire(ctx,payload):
                    reply=original(ctx,payload)
                    name=(payload.get('tools') or [{}])[0].get('function',{}).get('name')
                    if name in ('case_request_plan','proposal_prepare'):
                        call=reply['choices'][0]['message']['tool_calls'][0]['function']
                        import json
                        args=json.loads(call['arguments'])
                        if name=='case_request_plan':args=flat(args)
                        else:args['spec']['sources']=[dict(id=row['id']) for row in args['spec']['sources']]
                        call['arguments']=json.dumps(args,ensure_ascii=False)
                    return reply
                harness.transport=current_wire
                code,ctx=harness.cli(['request','--request-id','fixture_parameter_fallback','--project-id',harness.project_id,'--baseline-version','v000002','--requirement','把总质量改为92kg，增加部分在原重心，几何控制不变。'])
                self.assertEqual(code,0,ctx);harness.authorize('fixture_parameter_fallback')
                code,planned=harness.cli(['plan-request','--request-id','fixture_parameter_fallback']);self.assertEqual(code,0,planned)
                code,confirmed=harness.cli(['confirm-request','--request-id','fixture_parameter_fallback','--displayed-digest',planned['displayed_digest'],'--approval-text','CONFIRM']);self.assertEqual(code,0,confirmed)
                worker=harness.flow('fixture_parameter_fallback')
                result=asyncio.run(worker.generate());usage=CaseBudget(task_context=worker.ctx).usage()
                self.assertEqual(usage['requests'],2);self.assertEqual(usage['counters']['candidate'],1)
                self.assertNotIn('origin',result);self.assertEqual(result['transport'],'ISOLATED_FIXTURE')
                self.assertEqual(asyncio.run(worker.generate()),result)
                self.assertEqual(CaseBudget(task_context=worker.ctx).usage()['requests'],2)
                self.assertFalse((worker.root/'parameter-edit-provenance.json').exists())
        finally:
            case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=saved


if __name__=='__main__':unittest.main()
