"""TEST_ONLY narrow protocol/reference/approved-quota checks; no HTTP/OMC."""
import copy,hashlib,json,sqlite3,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from week12.src.car_domain.model_artifact import ExecutionContext,validate_artifact,save,SI_REAL_ALIASES
from week12.src.car_domain.artifact_service import ArtifactService
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.artifact_reference import load_modeling_reference
from week12.src.car_domain.artifact_runtime import ArtifactRuntime,RuntimeError
from week12.tests import test_artifact_thinking as thinking_fixtures

ROOT=Path(__file__).resolve().parents[1]/'results/s5-fivefloor-proposal-20261012-01/continuation-test-only'

class ProtocolChecks(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/uuid.uuid4().hex;self.root.mkdir(parents=True)
        path=self.root/'context.json';save(path,dict(schema='v11-artifact-execution-context-1',context_id='TEST_ONLY_'+uuid.uuid4().hex,status='ACTIVE',mode='TEST_ONLY',allow_execution=False,max_tasks=1,worker_timeout_s=300,simulation_timeout_s=120))
        self.context=ExecutionContext.load(path)
        self.service=ArtifactService(self.root/'projects.sqlite','TEST_ONLY',context=self.context,worker_manager=object());self.service.project_create('TEST_ONLY_protocol')
        self.source=self.service.store.record_source('TEST_ONLY','TEST_ONLY mass2kg requirement')
        self.spec=dict(schema_version='v11-model-artifact-1',backend='model-artifact',artifact=dict(model_name='TestOnlyModel',modelica_source='model TestOnlyModel\n parameter Real mass=2;\n Real x;\nequation\n x=mass;\nend TestOnlyModel;'),sources=[dict(id=self.source['source_id'])],requirements=[dict(text='TEST_ONLY mass2kg',source_id=self.source['source_id'],source_text='mass2kg')],design=dict(summary='TEST_ONLY interface fixture',assumptions=[],parameters=[dict(name='mass',value=2,unit='kg',origin='model',basis='TEST_ONLY')]),libraries=[],settings=dict(stop_time=1,intervals=10),outputs=dict(x='TestOnlyModel.x'),checks=[])
    def canonical(self):return self.service.normalize_candidate(self.spec)[0]
    def test_si_aliases_and_description_preserve_literal_values(self):
        for alias in sorted(SI_REAL_ALIASES):
            spec=self.canonical();spec['artifact']['modelica_source']=spec['artifact']['modelica_source'].replace('Real mass=2;','Modelica.Units.SI.'+alias+' mass=2 "TEST_ONLY exact literal";')
            self.assertTrue(validate_artifact(spec)['ready'],alias)
        for declaration in ['SomeCustomType mass=2;', 'Modelica.Units.SI.UnknownType mass=2;', 'Modelica.Units.SI.Mass mass=1+1;', 'Modelica.Units.SI.Mass mass=3;']:
            spec=self.canonical();spec['artifact']['modelica_source']=spec['artifact']['modelica_source'].replace('Real mass=2;',declaration)
            self.assertFalse(validate_artifact(spec)['ready'],declaration)
    def test_standard_torque_aliases(self):
        for alias in ('Torque','MomentOfForce'):
            spec=self.canonical();spec['artifact']['modelica_source']=spec['artifact']['modelica_source'].replace('Real mass=2;','Modelica.Units.SI.'+alias+' mass=2 "TEST_ONLY torque literal";')
            self.assertTrue(validate_artifact(spec)['ready'])
    def test_source_ids_resolved_in_public_proposal_without_source_edit(self):
        before=copy.deepcopy(self.spec);result=self.service.proposal_prepare(self.spec)
        self.assertTrue(result['ok']);self.assertEqual(self.spec,before)
        frozen=self.service.store.proposal(result['proposal']['proposal_id'])['spec']
        self.assertEqual(frozen['sources'],[dict(id=self.source['source_id'],text=self.source['text'])])
        self.assertEqual(result['proposal']['state'],'awaiting_confirmation')
        with self.service.store.reading() as db:self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
    def test_explicit_sources_compatible_but_changed_or_unknown_rejected(self):
        self.spec['sources'][0]['text']=self.source['text'];self.assertTrue(validate_artifact(self.canonical())['ready'])
        self.spec['sources'][0]['text']+=' '
        with self.assertRaisesRegex(ValueError,'differs'):self.canonical()
        self.spec['sources']=[dict(id='src_unknown')]
        with self.assertRaisesRegex(ValueError,'unknown'):self.canonical()
    def make_reference(self):
        source=self.root/'reference.mo';source.write_text('model TEST_ONLY\n Real x;\nend TEST_ONLY;\n',encoding='utf8');raw=source.read_bytes();excerpt=' Real x;'
        item=dict(path=str(source),source_sha256=hashlib.sha256(raw).hexdigest(),line_start=2,line_end=2,excerpt_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),excerpt=excerpt,purpose='TEST_ONLY formula reference',limitations=['NOT USER FACTS'])
        path=self.root/'reference.json';save(path,dict(schema='v11-artifact-modeling-reference-1',purpose='TEST_ONLY',limitations=['NOT VEHICLE PASS'],items=[item]));return path,source
    def test_reference_shared_context_and_hash_drift(self):
        path,source=self.make_reference();cli=ArtifactCLI.__new__(ArtifactCLI);cli.read_only=False;cli.store=self.service.store;cli.session_id='TEST_ONLY';cli.context=self.context;cli.modeling_reference=path
        context=cli._context(self.service._current_project())
        self.assertEqual(context['trusted_sources'],[dict(id=self.source['source_id'],text=self.source['text'])]);self.assertEqual(context['modeling_reference']['kind'],'MODELING_REFERENCE_NOT_USER_FACTS_OR_VEHICLE_ACCEPTANCE')
        source.write_text('changed',encoding='utf8')
        with self.assertRaisesRegex(ValueError,'source drift'):load_modeling_reference(path)
    def test_cli_preflight_uses_same_id_resolution(self):
        cli=ArtifactCLI.__new__(ArtifactCLI);cli.store=self.service.store;cli.session_id='TEST_ONLY';cli.context=self.context
        cli._preflight_batch([('proposal_prepare',dict(spec=self.spec))]);self.assertIsNotNone(cli._candidate_digest)
        self.spec['sources'][0]['text']='modified'
        with self.assertRaisesRegex(ValueError,'differs'):cli._preflight_batch([('proposal_prepare',dict(spec=self.spec))])

class ContinuationBudgetChecks(unittest.TestCase):
    setUp=thinking_fixtures.ThinkingRuntimeTests.setUp
    complete=thinking_fixtures.ThinkingRuntimeTests.complete
    fixture=thinking_fixtures.KnownLengthRecoveryTests.fixture
    def two_attempts(self):
        r,path=self.fixture();r.recover_known_length(path)
        self.response['choices'][0]['finish_reason']='stop';self.response['usage']=dict(prompt_tokens=4,completion_tokens=8,total_tokens=12)
        self.complete(r,'second');return r,path
    def test_same_ledger_append_one_request_preserves_failed_history(self):
        r,path=self.two_attempts()
        with sqlite3.connect(r.ledger_path) as db:before=db.execute('SELECT * FROM requests').fetchall();old_recovery=db.execute("SELECT value FROM meta WHERE key='known_length_recovery'").fetchone()
        record=r.authorize_proposal_continuation(path,approval_verbatim='批准');self.assertEqual(record['maximum_new_requests'],1);self.assertEqual(r.config['max_requests'],3);self.assertEqual(r.config['max_total_tokens'],2000000);self.assertEqual(r.config['max_usd'],.10)
        fresh=ArtifactRuntime(r.config_path,r.ledger_path,transport=r.transport)
        with sqlite3.connect(r.ledger_path) as db:self.assertEqual(before,db.execute('SELECT * FROM requests').fetchall());self.assertEqual(old_recovery,db.execute("SELECT value FROM meta WHERE key='known_length_recovery'").fetchone())
        self.complete(fresh,'third')
        with self.assertRaisesRegex(RuntimeError,'budget exhausted'):self.complete(fresh,'fourth')
        self.assertEqual(len(self.calls),3)
    def test_frozen_unknown_or_unapproved_cannot_extend(self):
        r,path=self.two_attempts()
        with self.assertRaisesRegex(RuntimeError,'explicit approved'):r.authorize_proposal_continuation(path,approval_verbatim='not approved')
        r._mark_unknown('second','TEST_ONLY unknown');before=r.config_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'frozen'):r.authorize_proposal_continuation(path,approval_verbatim='批准')
        self.assertEqual(before,r.config_path.read_bytes());self.assertFalse((self.root/'proposal-continuation-authorization.json').exists())
    def test_third_config_without_bound_receipt_refused(self):
        self.config['max_requests']=3;path=self.root/'runtime.json';save(path,self.config)
        with self.assertRaisesRegex(RuntimeError,'original authorized ledger'):ArtifactRuntime(path,self.root/'new.sqlite',transport=lambda *_:self.fail('must never send'))

if __name__=='__main__':unittest.main()
