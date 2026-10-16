"""TEST_ONLY synthetic records; no provider, OMC, or vehicle simulation."""
import json,tempfile,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch
from week11.src.store import canonical_json
from week12.src.car_domain.artifact_service import ArtifactService
from week12.src.car_domain.model_artifact import save,sha,summarize_execution
from week12.tests.test_model_artifact import context,fixture,SOURCE_TEXT

OUT=Path(__file__).resolve().parents[1]/'results/s4-artifact-history-20261011-01'

class HistoryChecks(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(exist_ok=True);self.temp=tempfile.TemporaryDirectory(dir=OUT);self.root=Path(self.temp.name)
        self.patch=patch('week12.src.car_domain.artifact_history.WEEK12_ROOT',self.root);self.patch.start()
        self.db=self.root/'history.sqlite';ctx=context(self.root/'context.json',allow=False)
        self.writer=ArtifactService(self.db,'history_test',context=ctx,worker_manager=object())
        self.project=self.writer.project_create('TEST_ONLY_history')['project']
        source=self.writer.record_source(SOURCE_TEXT);self.source_id=source['source_id']
        self.left=self.record(1);self.right=self.record(2,tool_fail=True)
        self.reader=ArtifactService(self.db,'history_test',read_only=True)
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def record(self,index,tool_fail=False,incomplete=False):
        spec=fixture(self.source_id);spec['settings']['stop_time']=index
        if index>1:
            spec['artifact']['modelica_source']=spec['artifact']['modelica_source'].replace('0.6','0.7');spec['design']['parameters'][0]['value']=.7
        proposal=self.writer.proposal_prepare(spec)['proposal'];self.writer.confirm_local(proposal['proposal_id'],proposal['displayed_digest'])
        task,_=self.writer.store.create_task(proposal['proposal_id'],'history_test','synthetic_'+str(index))
        owner='owner_fixture_'+str(index);self.writer.store.reserve_launch(task['task_id'],owner);self.writer.store.claim_task(task['task_id'],owner,1)
        if incomplete:
            self.writer.store.finish_task(task['task_id'],'cancelled',owner=owner,error='TEST_ONLY cancellation fixture; no worker or evidence')
            return self.writer.store.task(task['task_id'])
        base=self.root/'projects'/task['project_id']/'versions'/task['version_id'];base.mkdir(parents=True)
        save(base/'spec.json',spec);save(base/'confirmation.json',self.writer.store.proposal(proposal['proposal_id'])['confirmation'])
        (base/'frozen-model.mo').write_text(spec['artifact']['modelica_source'],encoding='utf8',newline='\n')
        (base/'fake.csv').write_text('time,signal\n0,0\n1,0\n',encoding='utf8')
        status='FAIL' if tool_fail else 'PASS'
        outcome=summarize_execution(spec,dict(compile=dict(status=status),simulation=dict(status=status),test_only=True,toolchain_invocations=0),base/'fake.csv')
        outcome.update(test_only=True,toolchain_invocations=0)
        save(base/'outcome.json',outcome)
        files={p.relative_to(base).as_posix():dict(sha256=sha(p.read_bytes()),bytes=p.stat().st_size) for p in base.iterdir()}
        display=proposal['display'];manifest=base/'artifact-manifest.json'
        save(manifest,dict(schema='v11-artifact-manifest-1',binding=dict(task_id=task['task_id'],project_id=task['project_id'],spec_digest=proposal['spec_digest'],source_sha256=display['source_sha256'],owner=owner,context_id=display['execution_context_id'],context_sha256=display['execution_context_sha256']),outcome=outcome,files=files))
        result={**outcome,'outcome_manifest_path':str(manifest),'outcome_manifest_sha256':sha(manifest.read_bytes()),'worker_exit_code':0,'worker_pid':1}
        self.writer.store.finish_task(task['task_id'],'failed' if tool_fail else 'succeeded',owner=owner,result=result)
        return self.writer.store.task(task['task_id'])
    def change_result(self,task,change):
        result=dict(task['result']);change(result)
        with self.writer.store.transaction() as db:db.execute('UPDATE tasks SET result_json=? WHERE task_id=?',(canonical_json(result),task['task_id']))
    def test_snapshots_conditions_and_no_publication(self):
        before=sha(self.db.read_bytes());items=self.reader.versions_list()['versions']['items'];self.assertEqual(len(items),2)
        self.assertEqual([i['task_status'] for i in items],['FAIL','UNKNOWN']);self.assertTrue(all(i['published'] is False and i['read_only'] for i in items))
        compared=self.reader.versions_compare(self.left['version_id'],self.right['version_id'])['comparison']
        self.assertFalse(compared['differences']['source']['same']);self.assertFalse(compared['differences']['settings']['same']);self.assertFalse(compared['same_conditions'])
        self.assertEqual(compared['combined_vehicle_task_status'],'UNKNOWN');self.assertEqual(before,sha(self.db.read_bytes()))
        self.assertIsNone(self.reader.project_status()['status']['project']['current_version_id'])
    def test_incomplete_cancel_visible(self):
        third=self.record(3,incomplete=True);listed=self.reader.versions_list()['versions']['items'];self.assertEqual(len(listed),3)
        self.assertEqual(listed[-1]['evidence_status'],'INCOMPLETE');self.assertEqual(listed[-1]['state'],'cancelled')
        self.assertIsNone(self.reader.evidence_query('task:'+third['task_id'])['evidence']['manifest'])
        self.assertEqual(self.reader.versions_compare(self.left['version_id'],third['version_id'])['comparison']['evidence_status'],'INCOMPLETE')
    def test_staged_running_is_not_final_pass(self):
        staged={k:v for k,v in self.left['result'].items() if k not in ('worker_exit_code','worker_pid')}
        with self.writer.store.transaction() as db:db.execute("UPDATE tasks SET state='running',result_json=? WHERE task_id=?",(canonical_json(staged),self.left['task_id']))
        snap=self.reader.evidence_query('task:'+self.left['task_id'])['evidence']['snapshot']
        self.assertEqual(snap['evidence_status'],'STAGED');self.assertFalse(snap['recorded_conclusions_verified'])
        self.assertEqual((snap['chain_status'],snap['task_status']),('PENDING','UNKNOWN'))
    def test_readonly_no_initialization_worker_or_write(self):
        with patch('week12.src.car_domain.artifact_service.ArtifactStore',side_effect=AssertionError('write store created')):
            reader=ArtifactService(self.db,'history_test',read_only=True)
        self.assertIsNone(reader.context);self.assertIsNone(reader.worker_manager)
        with self.assertRaises(sqlite3.OperationalError):reader.project_create('forbidden')
        for call in (lambda:reader.execution_start(self.left['proposal_id'],'x'),lambda:reader.confirm_local(self.left['proposal_id'],'x'),lambda:reader.proposal_prepare(fixture())):
            with self.assertRaisesRegex(ValueError,'read-only'):call()
        with self.assertRaisesRegex(ValueError,'historical session'):ArtifactService(self.db,'missing',read_only=True)
        with self.assertRaisesRegex(ValueError,'does not exist'):ArtifactService(self.root/'absent.sqlite','missing',read_only=True)
        self.assertFalse((self.root/'absent.sqlite').exists());self.assertFalse(self.db.with_suffix('.sqlite.server.lock').exists())
    def test_wrong_project_and_version_refused(self):
        with self.assertRaisesRegex(ValueError,'version mismatch'):self.reader.evidence_query('task:'+self.left['task_id'],self.right['version_id'])
        with self.assertRaises(ValueError):self.reader.versions_compare('v999999',self.right['version_id'])
        self.writer.project_create('TEST_ONLY_other')
        with self.assertRaisesRegex(ValueError,'another project'):self.reader.evidence_query('task:'+self.left['task_id'])
        with self.assertRaises(ValueError):self.reader.versions_compare(self.left['version_id'],self.right['version_id'])
    def test_manifest_path_escape_refused(self):
        self.change_result(self.left,lambda r:r.update(outcome_manifest_path=self.right['result']['outcome_manifest_path'],outcome_manifest_sha256=self.right['result']['outcome_manifest_sha256']))
        with self.assertRaisesRegex(ValueError,'version root'):self.reader.evidence_query('task:'+self.left['task_id'])
    def test_source_file_tamper_refused(self):
        base=self.root/'projects'/self.left['project_id']/'versions'/self.left['version_id'];(base/'frozen-model.mo').write_text('tampered',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'artifact hash mismatch'):self.reader.evidence_query('task:'+self.left['task_id'])
    def test_source_and_proposal_drift_refused(self):
        with self.writer.store.transaction() as db:db.execute('UPDATE sources SET text=? WHERE source_id=?',('forged',self.source_id))
        with self.assertRaisesRegex(ValueError,'source drift'):self.reader.evidence_query('task:'+self.left['task_id'])
        with self.writer.store.transaction() as db:
            db.execute('UPDATE sources SET text=? WHERE source_id=?',(SOURCE_TEXT,self.source_id));db.execute('UPDATE proposals SET spec_digest=? WHERE proposal_id=?',('0'*64,self.left['proposal_id']))
        with self.assertRaisesRegex(ValueError,'proposal digest'):self.reader.versions_list()
    def test_result_conclusion_and_manifest_identity_tamper_refused(self):
        self.change_result(self.left,lambda r:r.update(task_status='PASS'))
        with self.assertRaisesRegex(ValueError,'semantic drift'):self.reader.evidence_query('task:'+self.left['task_id'])
        self.change_result(self.left,lambda r:None) # Restore committed original data in test copy.
        path=Path(self.left['result']['outcome_manifest_path']);manifest=json.loads(path.read_bytes());manifest['binding']['project_id']='wrong';save(path,manifest)
        self.change_result(self.left,lambda r:r.update(outcome_manifest_sha256=sha(path.read_bytes())))
        with self.assertRaisesRegex(ValueError,'identity/spec/source'):self.reader.evidence_query('task:'+self.left['task_id'])

if __name__=='__main__':unittest.main()
