"""Narrow generation suffix transport tests; no HTTP, MCP or compiler."""
from copy import deepcopy
import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from week13.src import case_paths,case_strict_tools as strict
from week13.src.case_runtime import CaseRuntime
from week13.tests.test_case_strict_tools import reply,MemoryBudget

ROOT=case_paths.ROOT/'results/generation-protocol-repair-20261028-01'
TOOL=dict(type='function',function=dict(name='proposal_prepare',description='TEST_ONLY',parameters=dict(type='object',properties=dict(spec=dict(type='object',properties=dict(source=dict(type='string'),m=dict(type='number')),required=['source','m'],additionalProperties=False)),required=['spec'],additionalProperties=False)))

def calls(text,name='proposal_prepare'):
    return [dict(id='TEST_ONLY_call',type='function',function=dict(name=name,arguments=text))]

class GenerationFinalBrace(unittest.TestCase):
    def test_exact_one_final_brace_preserves_source_bytes_values_and_original(self):
        value={'spec':{'source':'model A\n // } quote " literal \\n 中文\nend A;','m':85.0}}
        raw=' \n'+json.dumps(value,ensure_ascii=False)+' \n}\t'
        original=calls(raw);before=deepcopy(original)
        derived,audit=strict.generation_suffix_calls(original,'tool_calls')
        self.assertEqual(original,before)
        self.assertEqual(strict.strict_json(derived[0]['function']['arguments']),value)
        offset=audit['character_offset'];self.assertEqual(raw[:offset]+raw[offset+1:],derived[0]['function']['arguments'])
        self.assertEqual(audit['original_arguments_utf8_sha256'],hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(audit['utf8_byte_offset'],len(raw[:offset].encode()))
        decoded,mapping=strict.decode_calls(original,[TOOL],finish_reason='tool_calls')
        self.assertEqual(json.loads(decoded[0]['function']['arguments']),value)
        self.assertEqual(mapping[0]['changes'][0],audit)
    def test_truncated_multiple_text_duplicate_and_nonfinite_are_rejected(self):
        invalid=['{"spec":{','{"spec":{}}}}','{"spec":{}} {}','{"spec":{}} trailing',
                 '{"spec":{},"spec":{}}}','{"spec":{"m":1,"m":2}}}',
                 '{"spec":{"m":NaN}}}','{"spec":{"m":Infinity}}}','{"spec":{"m":1e999}}}',
                 '[{"spec":{}}]}','{"spec":"{}"}}']
        for raw in invalid:
            with self.subTest(raw=raw),self.assertRaises(ValueError):strict.decode_calls(calls(raw),[TOOL],finish_reason='tool_calls')
    def test_wrong_tool_multiple_calls_and_incomplete_finish_rejected(self):
        raw='{"spec":{"source":"same","m":85}}}'
        for current,reason in [(calls(raw,'other'),'tool_calls'),(calls(raw)*2,'tool_calls'),(calls(raw),'length'),(calls(raw),'stop'),(calls(raw),None)]:
            with self.subTest(reason=reason,count=len(current)),self.assertRaises(ValueError):strict.decode_calls(current,[TOOL],finish_reason=reason)
    def test_internal_schema_is_still_authoritative_after_suffix(self):
        for spec in [{'source':'same'}, {'source':'same','m':'85'}, {'source':'same','m':85,'extra':'forbidden'}]:
            with self.subTest(spec=spec),self.assertRaises(ValueError):strict.decode_calls(calls(json.dumps({'spec':spec})+'}'),[TOOL],finish_reason='tool_calls')
    def test_runtime_accepts_only_complete_envelope_and_retains_original_protocol(self):
        raw='{"spec":{"source":"unchanged\\n源码","m":85}}}'
        protocol=reply(raw,name='proposal_prepare');before=deepcopy(protocol)
        runtime=CaseRuntime.__new__(CaseRuntime);runtime.config=case_paths.read_json(case_paths.CONFIG)
        runtime._usage_cost=lambda value:(value['usage'],0.0)
        result,_,_=runtime._parse(protocol)
        self.assertEqual(protocol,before);self.assertEqual(result['wire_tool_calls'],protocol['choices'][0]['message']['tool_calls'])
        self.assertEqual(result['assistant_message']['tool_calls'],result['wire_tool_calls'])
        self.assertEqual(json.loads(result['tool_calls'][0]['function']['arguments'])['spec']['source'],'unchanged\n源码')
        for change in ('length','stop'):
            bad=deepcopy(protocol);bad['choices'][0]['finish_reason']=change
            with self.subTest(reason=change),self.assertRaises(ValueError):runtime._parse(bad)
    def test_new_runtime_complete_uses_strict_schema_and_caches_bound_audit(self):
        ROOT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='TEST_ONLY_',dir=ROOT) as temp:
            runtime=CaseRuntime.__new__(CaseRuntime);runtime.config=case_paths.read_json(case_paths.CONFIG)
            runtime.config.update(tool_output_profile=strict.STRICT_PROFILE,api_base=strict.STRICT_BASE)
            runtime.task_context=None;runtime.transport=lambda *unused:reply('{"spec":{"source":"same","m":85}}}',name='proposal_prepare')
            runtime.run=Path(temp);runtime.ledger_path=Path(temp)/'fixture.sqlite';runtime.authorization={};runtime.budget=MemoryBudget()
            runtime._require_time=lambda:None;runtime._remaining=lambda:100
            runtime._usage_cost=lambda value:(value['usage'],0.0)
            result=runtime.complete([dict(role='system',content='TEST_ONLY')],[TOOL],'TEST_ONLY_generate_1')
            self.assertIn('generation_arguments_normalization',result)
            self.assertEqual(runtime.budget.rows['TEST_ONLY_generate_1']['status'],'succeeded')
            self.assertIn('generation_arguments_normalization',json.loads(runtime.budget.rows['TEST_ONLY_generate_1']['response']))
            runtime.transport=lambda *unused:reply('{"spec":{"source":"same","m":"bad"}}}',name='proposal_prepare')
            with self.assertRaises(ValueError):runtime.complete([dict(role='system',content='TEST_ONLY')],[TOOL],'TEST_ONLY_generate_2')
            self.assertEqual(runtime.budget.rows['TEST_ONLY_generate_2']['status'],'rejected')

if __name__=='__main__':unittest.main()
