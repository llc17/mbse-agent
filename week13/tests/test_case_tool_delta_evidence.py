"""Application delta evidence and independent reassembly; fake HTTP only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx

from week13.src.case_paths import ROOT,sha_value
from week13.src.case_stream import StreamRecorder,StreamProtocolError,complete_stream,reconstruct_tool_delta_evidence

PREP=ROOT/'results/strict-plan-protocol-preparation-20261022-02'


class BytePieces(httpx.SyncByteStream):
    def __init__(self,data):self.data=data
    def __iter__(self):
        # Every boundary, including Unicode UTF-8 and JSON escape boundaries,
        # is deliberately independent from SSE event boundaries.
        for offset in range(0,len(self.data),127):yield self.data[offset:offset+127]


class ToolDeltaEvidence(unittest.TestCase):
    def setUp(self):
        PREP.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='delta-',dir=PREP);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.payload=dict(model='deepseek-flash',stream=True,stream_options=dict(include_usage=True))
    def stream(self,folder=None,*,done=True):
        directory=folder or self.root
        recorder=StreamRecorder(directory,request_id='fixture',payload_sha256=sha_value(self.payload))
        args=['{"source":"轮'+('原'*30000)+'\\n\\u4e2d\\\""}', '{"packet":{"design":{"scope":{}}}}']
        cuts=[len(args[0])-7,19]
        deltas=[dict(reasoning_content='SECRET_REASONING',tool_calls=[dict(index=1,id='c',type='function',function=dict(name='two',arguments=args[1][:cuts[1]]))]),
                dict(tool_calls=[dict(index=0,id='a',type='function',function=dict(name='one',arguments=args[0][:cuts[0]]))]),
                dict(tool_calls=[dict(index=1,id='2',function=dict(arguments=args[1][cuts[1]:])),dict(index=0,id='1',function=dict(arguments=args[0][cuts[0]:]))])]
        data=[]
        for delta in deltas:
            chunk=dict(id='fixture_response',model='deepseek-flash',created=1,object='chat.completion.chunk',choices=[dict(index=0,delta=delta,finish_reason=None)])
            data.append('data: '+json.dumps(chunk,ensure_ascii=False)+'\n\n')
        chunk['choices']=[dict(index=0,delta={},finish_reason='tool_calls')];chunk['usage']=dict(prompt_tokens=5,completion_tokens=8,total_tokens=13)
        data.append('data: '+json.dumps(chunk)+'\n\n')
        if done:data.append('data: [DONE]\n\n')
        raw=''.join(data).encode('utf8')
        client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,headers={'content-type':'text/event-stream','x-private-header':'SECRET_HEADER'},stream=BytePieces(raw))))
        self.addCleanup(client.close)
        return recorder,client,args,deltas
    def complete(self,recorder,client):
        return complete_stream(client,url='https://fixture.invalid',headers={'Authorization':'SECRET_HEADER'},payload=self.payload,emit=recorder,request_id='fixture')
    def reconstruct(self,path,response):
        return reconstruct_tool_delta_evidence(path,request_id='fixture',payload_sha256=sha_value(self.payload),protocol_response=response)
    def test_unicode_escapes_interleaved_indices_and_large_arguments_reassemble_exactly(self):
        recorder,client,args,deltas=self.stream();response=self.complete(recorder,client)
        path=self.root/'tool-call-deltas.jsonl';evidence=self.reconstruct(path,response)
        self.assertEqual([item['function']['arguments'] for item in evidence['tool_calls']],args)
        self.assertGreater(path.stat().st_size,65536)
        text=path.read_text(encoding='utf8');self.assertNotIn('SECRET_REASONING',text);self.assertNotIn('SECRET_HEADER',text)
        records=[json.loads(line) for line in text.splitlines()]
        self.assertEqual([item['tool_calls'] for item in records[1:-1]],[item['tool_calls'] for item in deltas])
        self.assertEqual(json.loads(args[1])['packet']['design'],{'scope':{}})
        self.assertEqual(response['choices'][0]['message']['tool_calls'][1]['function']['arguments'],args[1])
        self.assertNotIn('scope',json.loads(args[1])['packet'])
        for name in ('progress.json','timing.json'):
            progress=json.loads((self.root/name).read_text(encoding='utf8'))
            self.assertNotIn('tool_calls',progress)
            self.assertNotIn('arguments',progress)
    def test_reordered_missing_partial_or_interrupted_records_never_complete(self):
        recorder,client,_,_=self.stream();response=self.complete(recorder,client)
        lines=(self.root/'tool-call-deltas.jsonl').read_text(encoding='utf8').splitlines(keepends=True)
        variants=[lines[:1]+[lines[2],lines[1]]+lines[3:],lines[:2]+lines[3:],lines[:-1],lines[:-1]+[lines[-1][:-2]]]
        interrupted=deepcopy([json.loads(line) for line in lines]);interrupted[-1]['complete']=False
        for index,variant in enumerate(variants):
            path=self.root/('bad-'+str(index)+'.jsonl');path.write_text(''.join(variant),encoding='utf8')
            with self.subTest(index=index),self.assertRaises((ValueError,KeyError)):self.reconstruct(path,response)
        other=self.root/'interrupted';other.mkdir();recorder2,client2,_,_=self.stream(other,done=False)
        with self.assertRaises(StreamProtocolError):self.complete(recorder2,client2)
        recorder2('STREAM_TERMINATED',classification='TRANSPORT_ERROR',error_type='StreamProtocolError')
        with self.assertRaisesRegex(StreamProtocolError,'INCOMPLETE'):self.reconstruct(other/'tool-call-deltas.jsonl',response)
    def test_payload_protocol_drift_or_evidence_write_failure_is_not_success(self):
        recorder,client,_,_=self.stream();response=self.complete(recorder,client)
        path=self.root/'tool-call-deltas.jsonl'
        with self.assertRaisesRegex(StreamProtocolError,'BINDING'):reconstruct_tool_delta_evidence(path,request_id='other',payload_sha256=sha_value(self.payload),protocol_response=response)
        changed=deepcopy(response);changed['choices'][0]['message']['tool_calls'][0]['function']['arguments']+=' '
        with self.assertRaisesRegex(StreamProtocolError,'ARGUMENTS'):self.reconstruct(path,changed)
        folder=self.root/'write-error';folder.mkdir();recorder2,client2,_,_=self.stream(folder)
        with patch.object(recorder2,'_append_tool_evidence',side_effect=OSError('TEST_ONLY disk write failure')):
            with self.assertRaises(OSError):self.complete(recorder2,client2)
        with self.assertRaisesRegex(StreamProtocolError,'INCOMPLETE'):self.reconstruct(folder/'tool-call-deltas.jsonl',response)


if __name__=='__main__':unittest.main()
