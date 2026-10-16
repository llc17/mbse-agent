"""TEST_ONLY timeout settings: mocked HTTP client; no network or credentials."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from week12.src.car_domain.artifact_runtime import ArtifactRuntime,RuntimeError

ROOT=Path(__file__).resolve().parents[1]/'results/s5-my-run-timeout-20261013-01/test-only'

class ReadTimeoutTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=dict(schema_version='v10-runtime-1',model='deepseek-flash',api_base='https://api.deepseek.com',thinking='enabled',temperature=0,max_tokens=16384,timeout_s=60,max_requests=2,max_total_tokens=2000000,max_usd=.10,max_input_tokens=64000,input_usd_per_million=.3,output_usd_per_million=1.2,max_consecutive_transport_errors=1,automatic_retries=0,budget_profile='thinking-task-20261012')

    def runtime(self,**kwargs):
        path=self.root/'runtime.json';path.write_text(json.dumps(self.config),encoding='utf8')
        return ArtifactRuntime(path,self.root/'runtime.sqlite',**kwargs)

    def send_settings(self,runtime):
        with patch('week12.src.car_domain.artifact_runtime._api_key',return_value='TEST_ONLY'),patch('httpx.Client') as factory:
            client=factory.return_value.__enter__.return_value
            client.post.return_value.json.return_value={'TEST_ONLY':True}
            self.assertEqual(runtime._send([],[]),{'TEST_ONLY':True})
            self.assertNotIn('stream',client.post.call_args.kwargs['json'])
            self.assertFalse(factory.call_args.kwargs['follow_redirects'])
            client.post.assert_called_once()
            return factory.call_args.kwargs['timeout'].as_dict()

    def test_default_preserves_timeout_and_injected_transport(self):
        runtime=self.runtime()
        self.assertEqual(self.send_settings(runtime),dict(connect=60,read=60,write=60,pool=60))
        self.assertNotIn('read_timeout_s',runtime.config)
        calls=[]
        runtime.transport=lambda payload,key,timeout:calls.append(timeout) or {'TEST_ONLY':True}
        with patch('week12.src.car_domain.artifact_runtime._api_key',return_value='TEST_ONLY'):
            runtime._send([],[])
        self.assertEqual(calls,[60])

    def test_explicit_600_changes_read_only(self):
        self.config['read_timeout_s']=600
        runtime=self.runtime()
        self.assertEqual(self.send_settings(runtime),dict(connect=60,read=600,write=60,pool=60))
        calls=[]
        runtime.transport=lambda payload,key,timeout:calls.append(timeout) or {'TEST_ONLY':True}
        with patch('week12.src.car_domain.artifact_runtime._api_key',return_value='TEST_ONLY'):
            runtime._send([],[])
        self.assertEqual(calls,[60])

    def test_invalid_read_timeout_rejected_before_ledger_creation(self):
        for value in (0,601,-1,True,False,600.0,'600',None):
            with self.subTest(value=value):
                self.config['read_timeout_s']=value
                with self.assertRaisesRegex(RuntimeError,'read_timeout_s'):
                    self.runtime()
                self.assertFalse((self.root/'runtime.sqlite').exists())

if __name__=='__main__':unittest.main()
