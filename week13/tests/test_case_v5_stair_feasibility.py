"""Readonly v5 + in-memory H/D projections. No product output or new request.

This checks eligibility/interfaces only, not physics, account access or new
execution permission. The real Store and all historical files remain readonly.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from week13.src import case_scope, case_taskflow
from week13.src.case_backend import service, module
from week13.src.case_paths import ROOT, read_json
from week13.src.case_requests import load_request
from week13.src.case_projects import support_for_source
from week13.src.case_strict_tools import generation_tool, prepare_tools, transport_profile
from week13.src.inherited import artifact_baseline


class V5StairFeasibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin=load_request('contact_numeric85_150_280_20261022_01')
        cls.svc=service(cls.origin['db_path'],cls.origin['session_id'],read_only=True,task_context=cls.origin)
        cls.snapshot=module('artifact_service').ArtifactService._artifact_version(cls.svc,'v000005')
        cls.ref=artifact_baseline.reference(cls.snapshot)
        base=Path(cls.origin['project_root'])/'projects'/cls.origin['project_id']/'versions/v000005'
        paths=[Path(cls.origin['db_path']),Path(cls.origin['root'])/'task-context.json',
               base/'artifact-manifest.json',base/'frozen-model.mo',base/'frozen-model.sysml',base/'outcome.json']
        cls.protected={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    @classmethod
    def tearDownClass(cls):
        for path,expected in cls.protected.items():
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                raise AssertionError('readonly fixture changed historical file: '+str(path))
    @staticmethod
    def parameter(source,name,value):
        pattern=r'(\bparameter\s+(?:Real|Integer|Modelica\.Units\.SI\.\w+)\s+'+re.escape(name)+r'\s*=\s*)('+case_scope.NUMBER+r')'
        result,count=re.subn(pattern,lambda match:match[1]+str(value),source)
        if count!=1:raise AssertionError('fixture shared parameter is not unique: '+name)
        return result
    def fixture(self,height,depth):
        base=deepcopy(self.snapshot['frozen_spec']);spec=deepcopy(base)
        raw='LOCAL_FIXTURE_ONLY inherit v5 85kg; stepH='+str(height)+' stepD='+str(depth)
        source=dict(id='fixture_not_stored_source',text=raw)
        requirements=deepcopy(base['requirements'])+[dict(text=raw,source_id=source['id'],source_text=raw)]
        sources=deepcopy(base['sources'])+[source]
        values=case_scope.modelica_parameters(base['artifact']['modelica_source'])[0]
        after={key:values[key] for key in case_scope.STAIR_PLAN_PARAMETERS};after.update(stepH=height,stepD=depth)
        design=deepcopy(base['design']);design['summary']='LOCAL_FIXTURE_ONLY projection of saved real v5 structure'
        for row in design['parameters']:
            if row['name'] in after:row['value']=after[row['name']]
        packet=dict(intent='CHANGE',support='SUPPORTED',missing=[],unsupported=[],requirements=requirements,
                    design=design,proposed_parameters=after,defaults=['LOCAL_FIXTURE_ONLY original vehicle/source/settings inherited'],
                    unknowns=['No new physical execution; capability UNKNOWN'],rationale='LOCAL_FIXTURE_ONLY interface projection',
                    scope=deepcopy(case_scope.STAIR_SCOPE_PACKET))
        ctx=dict(self.origin,request_id='fixture_not_persisted_v5_request',root=str(ROOT/'results/strict-output-contact-followup-20261022-01/not-created-context'),
                 session_id='fixture_not_persisted_session',baseline_version_id='v000005',baseline_ref=deepcopy(self.ref),
                 baseline_snapshot=deepcopy(self.snapshot),sources=sources,raw_request=raw,plan=packet,runtime_authority=None)
        spec.update(baseline_ref=deepcopy(self.ref),sources=sources,requirements=requirements,design=design)
        for name,value in (('stepH',height),('stepD',depth)):
            spec['artifact']['modelica_source']=self.parameter(spec['artifact']['modelica_source'],name,value)
            pattern=r'(\battribute\s+'+name+r'\s*:\s*Real\s*=\s*)('+case_scope.NUMBER+r')'
            sysml,count=re.subn(pattern,lambda match:match[1]+str(value),spec['artifact']['sysml_source'])
            if count!=1:raise AssertionError('fixture SysML shared parameter is not unique: '+name)
            spec['artifact']['sysml_source']=sysml
        entry=dict(requirements[-1],model_name=spec['artifact']['model_name'])
        block='requirement req_'+str(len(requirements))+' {'+''.join('attribute '+key+' : String = '+json.dumps(value,ensure_ascii=False)+';' for key,value in entry.items())+'}\n'
        spec['artifact']['sysml_source']=re.sub(r'}\s*$',lambda match:block+'}\n',spec['artifact']['sysml_source'],count=1)
        return ctx,packet,spec
    def test_actual_v5_is_eligible_by_complete_tool_chain_evidence_not_task_success(self):
        record=artifact_baseline.resolve(self.svc,self.ref)
        self.assertEqual(record['evidence_status'],'COMPLETE')
        self.assertEqual(record['chain_status'],'PASS')
        self.assertEqual(record['task_status'],'FAIL')
        self.assertEqual(record['outcome']['mission_assessment']['completion_status'],'OBSERVATION_INTERVAL_INCOMPLETE')
        self.assertEqual(support_for_source(record['frozen_spec']['artifact']['modelica_source'])['status'],'SUPPORTED')
    def test_two_new_stair_inputs_pass_existing_delta_and_dual_source_checks_locally(self):
        for height,depth in ((.18,.26),(.3,.26)):
            with self.subTest(height=height,depth=depth):
                ctx,packet,spec=self.fixture(height,depth)
                plan=case_scope.validate_request_plan(packet,ctx['baseline_snapshot']['frozen_spec'],ctx['sources'],task_context=ctx)
                self.assertTrue(plan['executable'])
                self.assertTrue(case_taskflow.baseline_sysml_available(ctx))
                self.assertEqual(case_taskflow.baseline_sysml_for_context(ctx),self.snapshot['frozen_spec']['artifact']['sysml_source'])
                case_taskflow.validate_import_plan(ctx,packet)
                # Selection is an in-memory fixture only. Resolve itself reads
                # the actual v5 Store/manifest through the original RO service.
                with patch.object(artifact_baseline,'selection',return_value=self.ref):
                    delta=case_scope.check_request_delta(self.svc,spec,ctx)
                self.assertEqual(delta['direct_parameters'],list(case_scope.STAIR_PARAMETERS))
                checked=module('model_artifact').validate_artifact(spec,require_sysml=True)
                self.assertTrue(checked['ready'],checked)
                contract={key:spec[key] for key in ('outputs','trajectory_contract') if key in spec}
                tools,_=prepare_tools([generation_tool(case_taskflow.GEN_TOOL,contract,needs_baseline=False)])
                self.assertTrue(tools[0]['function']['strict'])
                self.assertTrue(transport_profile(read_json(ROOT/'configs/case_runtime.json'))['strict_tools'])
                self.assertIsNone(ctx['runtime_authority'])
    def test_non_height_depth_source_change_is_rejected(self):
        ctx,packet,spec=self.fixture(.18,.26)
        spec['artifact']['modelica_source']=self.parameter(spec['artifact']['modelica_source'],'m',86)
        with patch.object(artifact_baseline,'selection',return_value=self.ref):
            with self.assertRaisesRegex(ValueError,'frozen physics|parameter differs'):
                case_scope.check_request_delta(self.svc,spec,ctx)


if __name__=='__main__':unittest.main()
