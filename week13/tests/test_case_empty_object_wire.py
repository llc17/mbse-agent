"""Exact empty-object transport mapping; no product or ledger access."""
from copy import deepcopy
import json,unittest
from week13.src.case_taskflow import PLAN_TOOL
from week13.src.case_scope import MASS_SCOPE_PACKET
from week13.src.case_strict_tools import EMPTY_MARKER,prepare_tools,decode_calls,validate_value
from week13.tests import test_case_strict_tools as fixtures


class EmptyObjectWire(unittest.TestCase):
    def args(self,support):
        packet=dict(intent='CHANGE',support=support,missing=['mass'] if support=='NEEDS_INPUT' else [],unsupported=['geometry'] if support=='UNSUPPORTED' else [],
                    requirements=[],design={},proposed_parameters={},defaults=[],unknowns=[],rationale='fixture',scope={})
        if support=='SUPPORTED':
            parameters=dict(m=85,I_pitch=20,T_main_max=60,T_nose_max=25)
            packet.update(requirements=[dict(text='fixture',source_id='fixture',source_text='literal '+json.dumps(EMPTY_MARKER))],
                design=dict(summary='source text with '+json.dumps(EMPTY_MARKER)+' and \\n',assumptions=[],parameters=[dict(name=k,value=v,unit='1',origin='model',basis='fixture') for k,v in parameters.items()]),
                proposed_parameters=parameters,scope=deepcopy(MASS_SCOPE_PACKET))
        args=dict(packet=packet);wire=deepcopy(args)
        for key in ('design','proposed_parameters','scope'):
            if wire['packet'][key]=={}:wire['packet'][key]=deepcopy(EMPTY_MARKER)
        return args,wire
    def calls(self,args):return [dict(id='fixture',type='function',function=dict(name='case_request_plan',arguments=json.dumps(args,ensure_ascii=False)))]
    def test_supported_missing_and_refused_branches_roundtrip_without_business_change(self):
        tools,audit=prepare_tools([PLAN_TOOL])
        for support in ('SUPPORTED','NEEDS_INPUT','UNSUPPORTED'):
            with self.subTest(support=support):
                internal,wire=self.args(support);calls=self.calls(wire);before=deepcopy(calls)
                validate_value(wire,tools[0]['function']['parameters'])
                decoded,mapping=decode_calls(calls,[PLAN_TOOL])
                self.assertEqual(json.loads(decoded[0]['function']['arguments']),internal)
                self.assertEqual(calls,before)
                self.assertEqual(len(mapping),0 if support=='SUPPORTED' else 1)
    def test_no_propertyless_object_remains_in_wire_and_internal_schema_unchanged(self):
        before=deepcopy(PLAN_TOOL);tools,_=prepare_tools([PLAN_TOOL])
        def walk(value):
            if isinstance(value,dict):
                if value.get('type')=='object':self.assertTrue(value.get('properties'))
                for item in value.values():walk(item)
            elif isinstance(value,list):
                for item in value:walk(item)
        walk(tools);self.assertEqual(PLAN_TOOL,before)
    def test_wrong_marker_extra_fields_and_marker_at_nonempty_path_are_rejected(self):
        for replacement in ({'v12_empty_object':'WRONG'},dict(EMPTY_MARKER,extra=1),{}):
            _,wire=self.args('NEEDS_INPUT');wire['packet']['design']=replacement
            with self.subTest(replacement=replacement),self.assertRaises(ValueError):decode_calls(self.calls(wire),[PLAN_TOOL])
        _,wire=self.args('SUPPORTED');wire['packet']['requirements']=[deepcopy(EMPTY_MARKER)]
        with self.assertRaises(ValueError):decode_calls(self.calls(wire),[PLAN_TOOL])


class RuntimeMarkerPreservation(unittest.TestCase):
    setUp=fixtures.StrictToolsTests.setUp
    tearDown=fixtures.StrictToolsTests.tearDown
    runtime=fixtures.StrictToolsTests.runtime
    def test_runtime_preserves_raw_provider_arguments_and_returns_audited_internal_empty(self):
        rt=self.runtime();internal,wire=EmptyObjectWire().args('NEEDS_INPUT')
        arguments=json.dumps(wire,ensure_ascii=False);self.response=fixtures.reply(arguments)
        original=deepcopy(self.response)
        result=rt.complete([dict(role='system',content='fixture'),dict(role='user',content='literal '+json.dumps(EMPTY_MARKER))],[PLAN_TOOL],'fixture_marker')
        self.assertEqual(json.loads(result['tool_calls'][0]['function']['arguments']),internal)
        self.assertEqual(result['assistant_message']['tool_calls'][0]['function']['arguments'],arguments)
        self.assertEqual(result['wire_tool_calls'][0]['function']['arguments'],arguments)
        self.assertFalse(result['wire_empty_object_mapping'][0]['source_strings_changed'])
        self.assertEqual(self.response,original)


if __name__=='__main__':unittest.main()
