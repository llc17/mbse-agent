"""Isolated Store/Service fixture. No product HTTP, OMC, worker or simulation launch."""
import asyncio,copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from week13.src.case_backend import module
from week13.src import case_paths,case_session
from week13.tests.test_case_agent import fixture

class Lifecycle(unittest.TestCase):
    def test_imported_reference_is_not_task_and_failed_version_is_persistent(self):
        with tempfile.TemporaryDirectory(dir=case_paths.PACKAGE) as temp,ExitStack() as stack:
            root=Path(temp)
            stack.enter_context(patch.object(case_paths,'RUN',root));stack.enter_context(patch.object(case_session,'RUN',root))
            context_path=root/'context.json'
            context_path.write_text(json.dumps(dict(schema='v11-artifact-execution-context-1',context_id='fixture_only',status='ACTIVE',mode='TEST_ONLY',allow_execution=True,max_tasks=6,worker_timeout_s=1900,simulation_timeout_s=900)),encoding='utf8')
            context=module('model_artifact').ExecutionContext.load(context_path)
            class NoLaunch:
                def __init__(self):self.started=[]
                def start(self,task_id):self.started.append(task_id)
            manager=NoLaunch();cls=module('artifact_service').ArtifactService
            svc=cls(root/'store.sqlite','session_fixture',context=context,worker_manager=manager)
            svc.project_root=root
            project=svc.project_create('TEST_ONLY inherited lifecycle')['project']
            source=svc.record_source('fixture evidence');spec=fixture()
            spec['sources']=[dict(id=source['source_id'],text=source['text'])]
            spec['requirements'][0]['source_id']=source['source_id']
            spec['artifact']['sysml_source']=spec['artifact']['sysml_source'].replace('src_fixture',source['source_id'])
            checked=module('model_artifact').validate_artifact(spec,require_sysml=True);self.assertTrue(checked['ready'],checked)
            (root/'baseline-original.mo').write_bytes(spec['artifact']['modelica_source'].encode())
            manifest=dict(source_sha256=checked['source_sha256'],spec_digest=checked['spec_digest'])
            (root/'imported-manifest.json').write_text(json.dumps(manifest),encoding='utf8')
            record=dict(project_id=project['project_id'],task_id='reference_fixture',version_id='v000000',spec_digest=checked['spec_digest'],source_sha256=checked['source_sha256'],manifest_sha256=case_paths.sha_file(root/'imported-manifest.json'),frozen_spec=spec,record_kind='IMPORTED_REFERENCE',task_status='NOT_RUN')
            (root/'imported-reference.json').write_text(json.dumps(record),encoding='utf8')
            (root/'run.json').write_text(json.dumps(dict(history_success_scope='synthetic history, no task')),encoding='utf8')
            ref=module('artifact_baseline').reference(record);svc.store.select_artifact_baseline(svc.session_id,project['project_id'],ref)
            self.assertEqual(svc.store.tasks_for_project(project['project_id']),[])
            self.assertEqual(module('artifact_baseline').selected_record(svc)['task_status'],'NOT_RUN')
            candidate=copy.deepcopy(spec);candidate['baseline_ref']=ref
            result=svc.proposal_prepare(candidate);self.assertTrue(result['ok'],result)
            proposal=svc.store.proposal(result['proposal']['proposal_id'])
            svc.confirm_local(proposal['proposal_id'],proposal['displayed_digest'])
            started=svc.execution_start(proposal['proposal_id'],'fixture_execution');task=started['task']
            self.assertEqual(task['version_id'],'v000001');self.assertEqual(len(manager.started),1)
            svc.execution_start(proposal['proposal_id'],'fixture_execution');self.assertEqual(len(manager.started),1)
            svc.store.reserve_launch(task['task_id'],'owner_fixture');svc.store.claim_task(task['task_id'],'owner_fixture',12345)
            svc.store.finish_task(task['task_id'],'failed',owner='owner_fixture',result=dict(tool_status='PASS',chain_status='PASS',task_status='FAIL',vehicle_task_status='UNKNOWN',physical_acceptance_status='NOT_VERIFIED'),error='synthetic task failure, not physical simulation')
            reopened=cls(root/'store.sqlite',svc.session_id,read_only=True)
            reopened.project_root=root
            state=case_session.status(reopened)
            self.assertEqual(state['latest_execution']['version_id'],'v000001');self.assertEqual(state['latest_execution']['task_status'],'FAIL');self.assertIsNone(state['last_task_pass_snapshot'])
            from week11.src.mcp_server import create_mcp
            tools=asyncio.run(create_mcp(svc).list_tools());names={t.name for t in tools}
            self.assertTrue({'proposal_prepare','proposal_get','execution_start','execution_status'}<=names)
            self.assertFalse(any('confirm' in name for name in names))

    def test_repair_feedback_is_explicit_original_receipt(self):
        from week13.src.case_cli import _failure_feedback
        from week13.src import case_cli
        with tempfile.TemporaryDirectory(dir=case_paths.PACKAGE) as temp,patch.object(case_cli,'RUN',Path(temp)):
            root=Path(temp);rid='fixture_original';(root/'cli-turns').mkdir()
            (root/'generation-0-failure.json').write_text(json.dumps(dict(request_id=rid,error='invalid JSON')),encoding='utf8')
            response=dict(tool_calls=[dict(function=dict(name='proposal_prepare',arguments='{"spec":'))],content='')
            (root/'cli-turns'/(case_paths.sha_value(rid)+'-response.json')).write_text(json.dumps(response),encoding='utf8')
            feedback=_failure_feedback('generation',1)
            self.assertEqual(feedback['prior_response_tool_calls'][0]['function']['arguments'],'{"spec":')
            with self.assertRaises(ValueError):_failure_feedback('generation',2)

if __name__=='__main__':unittest.main()
