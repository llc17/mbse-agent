"""Finite source interpretation and isolated saved-cache replay; zero HTTP/tools."""
import asyncio, copy, hashlib, io, json, sqlite3, tempfile, time, unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from week12.src.car_domain.artifact_continuous import ContinuousCLI, DesignProtocolError, SourceBindingError, initial_rest_evidence, validate_commitment

BASE=Path(__file__).resolve().parents[2]
RUN=BASE/'week12/runs/curb-cart-01'
OUT=BASE/'week12/results/s5-curb-initial-rest-20261014-01'
FACTS={}
def read(path):return json.loads(path.read_text(encoding='utf8'))
def saved():
    record=read(next((RUN/'cli-continuations').glob('flow_*.json')))
    rid=record['request_id'];stem=hashlib.sha256(rid.encode()).hexdigest()
    return record,read(RUN/'cli-turns'/(stem+'-request.json')),read(RUN/'cli-turns'/(stem+'-response.json'))

class Harness(ContinuousCLI):
    def __init__(self,root,pointer,record,response,sent):
        self.session_id=record['session_id'];self.sources=copy.deepcopy(pointer['sources']);self.budget=copy.deepcopy(record['budget'])
        self.budget['runtime_ledger']=str(root/'absent.sqlite')
        self.continuous=True;self.read_only=False
        self.store=SimpleNamespace(db_path=root/'projects.sqlite',current_project=lambda _:dict(project_id=pointer['project_id']))
        self.context=SimpleNamespace(assert_current=lambda:None)
        with closing(sqlite3.connect((RUN/'runtime.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute('select status,response from requests where request_id=?',(pointer['request_id'],)).fetchone()
        self.runtime=SimpleNamespace(_request=lambda _:dict(status=row[0],response=row[1]))
        self.receipts=dict(response=response,request=sent)
        self._flow_pointer().write_text(json.dumps(dict(pointer,design_id=None),ensure_ascii=False),encoding='utf8')
    def _flow_sources(self):return copy.deepcopy(self.sources)
    def _flow_budget(self):return copy.deepcopy(self.budget)
    def _receipt(self,_,kind):return self.receipts.get(kind)
    async def submit_prepared_request(self,*args,**kwargs):raise AssertionError('unexpected model request')
    async def _call(self,*args,**kwargs):raise AssertionError('unexpected tool dispatch')

class Checks(unittest.TestCase):
    def test_explicit_rest_and_clipped_negation(self):
        item=dict(name='initial_speed',value=0,unit='m/s',source_id='src_test',source_text='静止起步')
        for source in ('小车从入口前静止起步，向前行驶。','从静止状态出发','初始状态为静止'):
            item['source_text']=source.split('，')[0]
            self.assertIsNotNone(initial_rest_evidence(item,source))
        for source in ('不是静止起步','不要求静止起步','可能静止起步','如果静止起步','是否静止起步？','最终静止起步','例如静止起步','“静止起步”只是例子','静止起步或继续滑行'):
            item['source_text']='静止起步';self.assertIsNone(initial_rest_evidence(item,source),source)
        for text in ('最终静止','静止','起步','初始速度不明'):
            item['source_text']=text;self.assertIsNone(initial_rest_evidence(item,text))
        item['source_text']='静止起步'
        for field,value,unit in (('initial_speed',1,'m/s'),('initial_speed',0,'kg'),('initial_position',0,'m'),('drive_force',0,'N')):
            item.update(name=field,value=value,unit=unit);self.assertIsNone(initial_rest_evidence(item,'静止起步'))

    def test_original_complete_commitment_and_exact_source(self):
        record,_,_=saved();evidence=[]
        validate_commitment(record['commitment'],record['sources'],source_evidence=evidence)
        self.assertEqual([e['field'] for e in evidence],['initial_speed'])
        self.assertEqual(evidence[0]['kind'],'initial_rest_zero_speed')
        bad=copy.deepcopy(record['commitment']);binding=next(b for b in bad['literal_bindings'] if b['name']=='initial_speed')
        binding['source_text']+=' UNTRUSTED'
        with self.assertRaises(SourceBindingError):validate_commitment(bad,record['sources'])
        FACTS['original_complete_commitment']='READY schema/source boundary only; no vehicle proof'

    def test_original_success_cache_local_replay_and_no_approval(self):
        record,sent,response=saved()
        pointer=dict(project_id=record['project_id'],sources=record['sources'],request_id=record['request_id'],messages=sent['messages'])
        with tempfile.TemporaryDirectory(dir=OUT) as td,redirect_stdout(io.StringIO()):
            h=Harness(Path(td),pointer,record,response,sent)
            a=asyncio.run(h.handle_line('/retry'));b=asyncio.run(h.handle_line('/retry'))
            self.assertEqual(a['design'],b['design']);self.assertEqual(a['design']['state'],'READY')
            self.assertEqual(a['design']['commitment'],record['commitment']);self.assertEqual(a['new_model_requests'],0)
            self.assertEqual(list(h._flow_root().glob('*-approval.json')),[])
        FACTS['original_isolated_cache_replay']='2 identical READY, unchanged commitment, no approval/request/tool'

    def test_current_cache_still_format_error_and_original_run_unchanged(self):
        record,_,_=saved();pointer=read(next((RUN/'cli-continuations').glob('current-*.json')))
        stem=hashlib.sha256(pointer['request_id'].encode()).hexdigest()
        sent=read(RUN/'cli-turns'/(stem+'-request.json'));response=read(RUN/'cli-turns'/(stem+'-response.json'))
        with tempfile.TemporaryDirectory(dir=OUT) as td,redirect_stdout(io.StringIO()):
            h=Harness(Path(td),pointer,record,response,sent)
            with self.assertRaises(DesignProtocolError):asyncio.run(h.handle_line('/retry'))
            self.assertEqual(list(h._flow_root().glob('*-approval.json')),[])
        pre=read(OUT/'preconditions.json')
        for name,sha in pre['run_file_sha256'].items():self.assertEqual(hashlib.sha256((RUN/name).read_bytes()).hexdigest(),sha,name)
        FACTS['current_run']='no current design_id; current cache JSON invalid; /retry remains format error; original run SHA unchanged'

if __name__=='__main__':
    OUT.mkdir(parents=True,exist_ok=True);log=io.StringIO();start=time.monotonic()
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Checks))
    report=dict(passed=result.wasSuccessful(),tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),elapsed_s=time.monotonic()-start,new_product_requests=0,worker=0,omc=0,original_run_read_only=True,facts=FACTS)
    (OUT/'verification-console.txt').write_text(log.getvalue(),encoding='utf8')
    (OUT/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(log.getvalue());print(json.dumps(report,ensure_ascii=True));raise SystemExit(not result.wasSuccessful())
