"""Bounded HTTP error fixtures only; no network, key loading or ledger."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import httpx
from week13.src.case_paths import ROOT,read_json,sha_value
from week13.src.case_stream import StreamRecorder,complete_stream,http_error_evidence,StreamProtocolError

PACKAGE=ROOT/'results/strict-output-contact-followup-20261022-01'
URL='https://api.deepseek.com/beta/chat/completions'
SECRET='sk-SYNTHETIC_ERROR_TEST_ONLY_123456789'


class Response:
    def __init__(self,body,status=400,fail=False):
        self.body=body;self.status_code=status;self.fail=fail
        self.headers={'content-type':'application/json','x-request-id':'fixture-provider-id','set-cookie':'SYNTHETIC_COOKIE'}
    def iter_bytes(self,chunk_size=8192):
        for offset in range(0,len(self.body),chunk_size):yield self.body[offset:offset+chunk_size]
        if self.fail:raise httpx.ReadTimeout('SYNTHETIC_PRIVATE_EXCEPTION')
    def raise_for_status(self):
        raise httpx.HTTPStatusError('SYNTHETIC_REJECTED',request=httpx.Request('POST',URL),response=httpx.Response(self.status_code))


class Client:
    def __init__(self,response):self.response=response;self.calls=0
    @contextmanager
    def stream(self,*args,**kwargs):
        self.calls+=1
        yield self.response


class HTTPErrorDiagnostics(unittest.TestCase):
    def setUp(self):
        PACKAGE.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='http-error-local-',dir=PACKAGE)
        self.folder=Path(self.temp.name)
        self.payload=dict(model='deepseek-flash',stream=True,stream_options=dict(include_usage=True))
        self.headers={'Authorization':'Bearer '+SECRET}
    def tearDown(self):
        self.assertTrue(self.folder.resolve().is_relative_to(PACKAGE.resolve()));self.temp.cleanup()
    def capture(self,response):
        recorder=StreamRecorder(self.folder,request_id='fixture')
        client=Client(response)
        with self.assertRaises(httpx.HTTPStatusError):
            complete_stream(client,url=URL,headers=self.headers,payload=self.payload,emit=recorder,
                            request_id='fixture',secret_values=(SECRET,),on_http_error=recorder.record_http_error)
        self.assertEqual(client.calls,1)
        self.assertFalse((self.folder/'protocol-response.json').exists())
        return read_json(self.folder/'http-error.json')
    def test_complete_400_json_preserves_code_message_and_request_binding_still_fails(self):
        body={'error':{'code':'invalid_function_parameters','message':'Unsupported property oneOf','type':'invalid_request_error'}}
        result=self.capture(Response(json.dumps(body).encode()))
        self.assertEqual(result['error_json'],body)
        self.assertEqual(result['request_id'],'fixture');self.assertEqual(result['http_status'],400)
        self.assertEqual(result['provider_request_id'],'fixture-provider-id')
        self.assertEqual(result['payload_sha256'],sha_value(self.payload))
        self.assertTrue(result['body_complete']);self.assertFalse(result['body_truncated'])
        self.assertFalse(result['product_response']);self.assertFalse(result['billing_usage_authoritative'])
    def test_text_nonjson_body_is_retained_without_becoming_provider_response(self):
        result=self.capture(Response(b'Invalid schema: unexpected key at packet.design. Not JSON.'))
        self.assertIn('unexpected key',result['body_text'])
        self.assertIsNone(result['error_json']);self.assertTrue(result['body_complete'])
        self.assertEqual(result['json_parse_status'],'NOT_COMPLETE_VALID_UTF8_JSON')
    def test_known_keys_header_echoes_nested_credentials_and_bearer_are_redacted(self):
        body={'error':{'code':'bad_schema','message':'Rejected '+SECRET},'headers':{'Authorization':'Bearer '+SECRET,'x-private':'SYNTHETIC_PRIVATE_HEADER'},
              'nested':{'api_key':SECRET,'access_token':'SYNTHETIC_ACCESS_TOKEN','password':'SYNTHETIC_PASSWORD'},'other':'Bearer SYNTHETIC_OTHER_TOKEN'}
        result=self.capture(Response(json.dumps(body).encode()))
        saved=(self.folder/'http-error.json').read_text(encoding='utf8')
        for text in (SECRET,'SYNTHETIC_PRIVATE_HEADER','SYNTHETIC_ACCESS_TOKEN','SYNTHETIC_PASSWORD','SYNTHETIC_OTHER_TOKEN','SYNTHETIC_COOKIE'):
            self.assertNotIn(text,saved)
        self.assertEqual(result['error_json']['error']['code'],'bad_schema')
        self.assertIn('[REDACTED]',saved)
        self.assertFalse(result['raw_body_persisted']);self.assertFalse(result['response_headers_persisted'])
    def test_nonjson_credential_patterns_and_truncated_secret_tail_are_redacted(self):
        raw=('Authorization: Bearer '+SECRET+'\napi_key=PRIVATE_TOKEN\nheaders: X-Private=SECRET_HEADER\n').encode()
        result=self.capture(Response(raw))
        self.assertNotIn(SECRET,result['body_text']);self.assertNotIn('PRIVATE_TOKEN',result['body_text']);self.assertNotIn('SECRET_HEADER',result['body_text'])
        prefix='diagnostic '+SECRET[:9]
        value=http_error_evidence(Response((prefix+'tail').encode()),request_id='fixture',payload=self.payload,endpoint=URL,
            headers=self.headers,secret_values=(SECRET,),maximum_bytes=len(prefix))
        self.assertNotIn(SECRET[:9],value['body_text']);self.assertTrue(value['body_truncated'])
    def test_large_or_interrupted_body_never_claims_complete_or_parsed_full_json(self):
        result=http_error_evidence(Response(b'{"error":"'+b'x'*70000+b'"}'),request_id='fixture',payload=self.payload,endpoint=URL,headers=self.headers)
        self.assertEqual(result['captured_original_prefix_bytes'],65536)
        self.assertTrue(result['body_truncated']);self.assertFalse(result['body_complete']);self.assertIsNone(result['error_json'])
        partial=http_error_evidence(Response(b'{"error":"small"}',fail=True),request_id='fixture',payload=self.payload,endpoint=URL,headers=self.headers)
        self.assertFalse(partial['body_complete']);self.assertEqual(partial['body_read_error'],'ReadTimeout')
        self.assertIsNone(partial['error_json'])
    def test_forged_usage_body_is_only_diagnostic_and_cannot_return_success(self):
        body={'model':'deepseek-flash','id':'forged','choices':[{'message':{'content':'PASS'},'finish_reason':'stop'}],
              'usage':{'total_tokens':0,'prompt_tokens':0,'completion_tokens':0}}
        value=self.capture(Response(json.dumps(body).encode()))
        self.assertFalse(value['product_response']);self.assertFalse(value['billing_usage_authoritative'])
        self.assertFalse((self.folder/'protocol-response.json').exists())
    def test_recorder_refuses_wrong_request_or_status_binding(self):
        recorder=StreamRecorder(self.folder,request_id='fixture');recorder('HTTP_STATUS',http_status=400)
        proof=http_error_evidence(Response(b'failure'),request_id='fixture',payload=self.payload,endpoint=URL,headers=self.headers)
        for field,value in (('request_id','foreign'),('http_status',200),('product_response',True)):
            with self.subTest(field=field),self.assertRaises(StreamProtocolError):recorder.record_http_error(dict(proof,**{field:value}))
        self.assertFalse((self.folder/'http-error.json').exists())
    def test_credential_echo_in_provider_request_id_cannot_leak_to_status_or_progress(self):
        response=Response(b'{"error":{"code":"bad_request"}}');response.headers['x-request-id']=SECRET
        value=self.capture(response)
        self.assertEqual(value['provider_request_id'],'[REDACTED]')
        for path in self.folder.glob('*.json'):self.assertNotIn(SECRET,path.read_text(encoding='utf8'))


if __name__=='__main__':unittest.main()
