"""Three local protocol checks using the saved response; no HTTP/MCP/Worker/OMC."""
import asyncio,gc,hashlib,io,json,sqlite3,tempfile,unittest
from contextlib import closing,redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from week12.src.car_domain.artifact_cli import ArtifactCLI
from week12.src.car_domain.artifact_dialogue import continuous_protocol_target,parse_batch
from week12.src.car_domain.artifact_continuous import compare_candidate,resumable_generation
from week12.src.car_domain.model_artifact import validate_artifact

WEEK12=Path(__file__).resolve().parents[1];RUN=WEEK12/'runs/my-run';OUT=WEEK12/'results/s5-my-run-schema-20261013-01/test-only'

class SyntheticProvider:
    def __init__(self,root,original_rid,original,corrected):
        self.ledger_path=root/'runtime.sqlite';self.cache={original_rid:deepcopy(original)};self.corrected=corrected;self.new_requests=[];self.config={}
    def complete(self,messages,tools,rid):
        if rid in self.cache:return deepcopy(self.cache[rid])
        self.new_requests.append(dict(rid=rid,messages=deepcopy(messages),tools=deepcopy(tools)))
        if len(self.new_requests)>1:raise AssertionError('TEST_ONLY unexpected second correction request')
        self.cache[rid]=deepcopy(self.corrected);return deepcopy(self.corrected)

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True,exist_ok=True);self.temp=tempfile.TemporaryDirectory(dir=OUT);self.addCleanup(self.temp.cleanup);self.addCleanup(gc.collect);self.root=Path(self.temp.name)
        manifest=json.loads((RUN/'run.json').read_text(encoding='utf8'));self.session=manifest['session']
        pointer_path=RUN/'cli-continuations'/('current-'+hashlib.sha256(self.session.encode()).hexdigest()+'.json');self.pointer=json.loads(pointer_path.read_text(encoding='utf8'))
        self.record=json.loads((RUN/'cli-continuations'/(self.pointer['design_id']+'.json')).read_text(encoding='utf8'));self.rid='flow_generate_'+self.record['displayed_digest']
        prefix=hashlib.sha256(self.rid.encode()).hexdigest();self.request=json.loads((RUN/'cli-turns'/(prefix+'-request.json')).read_text(encoding='utf8'));self.original=json.loads((RUN/'cli-turns'/(prefix+'-response.json')).read_text(encoding='utf8'))
        self.available={t['function']['name']:SimpleNamespace(inputSchema=t['function']['parameters']) for t in self.request['tools']}
        parsed=parse_batch(self.original,self.request['tools'],self.available,False);self.target=continuous_protocol_target(parsed,self.record['commitment']);self.assertIsNotNone(self.target)
        source=self.target['artifact']['modelica_source'];self.assertEqual(hashlib.sha256(source.encode()).hexdigest(),'cfdfe539a53655b9f416fa7b2bc74b4e08dd052ae69cea6ce5a6c21a702fbdbc');self.assertEqual(source.count('\n'),20)
        self.corrected=deepcopy(self.original);self.corrected['content']='TEST_ONLY implementation explanations moved to response content';self.corrected['tool_calls'][0]['function']['arguments']=json.dumps(dict(spec=self.target),ensure_ascii=False)
        folder=self.root/'cli-continuations';folder.mkdir()
        for path in (pointer_path,RUN/'cli-continuations'/(self.record['design_id']+'.json'),RUN/'cli-continuations'/(self.record['design_id']+'-approval.json')):(folder/path.name).write_bytes(path.read_bytes())
        self.original_files={p:p.read_bytes() for p in (RUN/'cli-turns'/(prefix+'-request.json'),RUN/'cli-turns'/(prefix+'-response.json'),RUN/'cli-continuations'/(self.record['design_id']+'.json'),RUN/'cli-continuations'/(self.record['design_id']+'-approval.json'))}
        turns=self.root/'cli-turns';turns.mkdir()
        (turns/(prefix+'-request.json')).write_bytes((RUN/'cli-turns'/(prefix+'-request.json')).read_bytes())

    def cli(self,corrected=None):
        cli=ArtifactCLI.__new__(ArtifactCLI);cli.runtime=SyntheticProvider(self.root,self.rid,self.original,corrected or self.corrected);cli.store=SimpleNamespace(db_path=self.root/'projects.sqlite',current_project=lambda session:dict(project_id=self.record['project_id']));cli.session_id=self.session
        cli.read_only=False;cli.continuous=True;cli.context=SimpleNamespace(allow_execution=True);cli.responses=[];cli.tool_results=[];cli.last_proposal_id=None;cli.last_task_id=None;cli._tools=self.available;cli.last_design_id=None;cli._flow_expected=None
        cli._flow_sources=lambda:deepcopy(self.record['sources']);cli._flow_budget=lambda:deepcopy(self.record['budget']);cli._existing_proposal_task=lambda proposal_id:None;cli._save_dialogue=lambda *args:None;cli._remember=lambda result:None;cli._print_delivery=lambda result:None
        cli.dispatched=[]
        def preflight(parsed):
            self.assertEqual(len(parsed),1);candidate=deepcopy(parsed[0][1]['spec']);trusted={s['id']:s['text'] for s in self.record['sources']}
            for source in candidate['sources']:
                if set(source)=={'id'}:source['text']=trusted[source['id']]
            checked=validate_artifact(candidate);self.assertTrue(checked['ready'],checked['issues']);compare_candidate(self.record['commitment'],checked['spec']);cli._candidate_digest=checked['spec_digest']
        cli._preflight_batch=preflight
        async def prepare(name,args):
            self.assertEqual(name,'proposal_prepare');cli.dispatched.append(deepcopy(args));return dict(ok=True,proposal=dict(proposal_id='TEST_ONLY_proposal'))
        cli._call=prepare
        return cli

    def test_cached_response_correction_and_proposal_handoff_replay(self):
        cli=self.cli();handoffs=[]
        def stop(proposal_id,record,approval,**kwargs):
            handoffs.append(proposal_id);raise ValueError('TEST_ONLY stopped before execution')
        with patch('week12.src.car_domain.artifact_service.ArtifactService',return_value=SimpleNamespace(confirm_design_delegated_local=stop)),redirect_stdout(io.StringIO()):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError,'stopped before execution'):asyncio.run(cli.handle_line('确认'))
        self.assertEqual(handoffs,['TEST_ONLY_proposal','TEST_ONLY_proposal']);self.assertEqual(len(cli.dispatched),1);self.assertEqual(len(cli.runtime.new_requests),1)
        self.assertEqual(cli.runtime.new_requests[0]['rid'],self.rid+'_format1')
        self.assertEqual(cli.dispatched[0]['spec']['artifact'],json.loads(self.original['tool_calls'][0]['function']['arguments'])['spec']['artifact'])
        self.assertTrue((self.root/'cli-turns'/(hashlib.sha256((self.rid+'_format1').encode()).hexdigest()+'-tool-results.json')).exists())
        with closing(sqlite3.connect(self.root/'projects.sqlite')) as db:
            db.execute('CREATE TABLE proposals(proposal_id TEXT,session_id TEXT,state TEXT,project_id TEXT)');db.execute('CREATE TABLE tasks(task_id TEXT)')
            db.execute('INSERT INTO proposals VALUES(?,?,?,?)',('TEST_ONLY_proposal',self.session,'awaiting_confirmation',self.record['project_id']));db.commit()
        self.assertTrue(resumable_generation(self.root,self.session))
        for path,value in self.original_files.items():self.assertEqual(path.read_bytes(),value)
    def test_substantive_change_or_corrected_source_change_refused(self):
        parsed=parse_batch(self.original,self.request['tools'],self.available,False)
        for kind in ('summary','missing_assumption','unknown_field'):
            changed=deepcopy(parsed);spec=changed[0][1]['spec']
            if kind=='summary':spec['design']['summary']+=' TEST_ONLY changed design'
            if kind=='missing_assumption':spec['design']['assumptions'].pop(0)
            if kind=='unknown_field':spec['unexpected_physics']=1
            self.assertIsNone(continuous_protocol_target(changed,self.record['commitment']))
        changed=deepcopy(self.corrected);args=json.loads(changed['tool_calls'][0]['function']['arguments']);args['spec']['artifact']['modelica_source']+='\n';changed['tool_calls'][0]['function']['arguments']=json.dumps(args,ensure_ascii=False)
        cli=self.cli(changed)
        with self.assertRaisesRegex(ValueError,'changed locked source'):asyncio.run(cli.handle_line('确认'))
        self.assertEqual(cli.dispatched,[]);self.assertEqual(len(cli.runtime.new_requests),1)
    def test_failed_single_correction_cached_no_second_attempt(self):
        broken=deepcopy(self.corrected);broken['tool_calls'][0]['function']['arguments']='{';cli=self.cli(broken)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'one format correction failed'):asyncio.run(cli.handle_line('确认'))
        self.assertEqual(cli.dispatched,[]);self.assertEqual(len(cli.runtime.new_requests),1)

if __name__=='__main__':unittest.main()
