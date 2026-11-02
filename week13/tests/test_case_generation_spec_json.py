"""Strict public spec-string compatibility, using isolated stores and fake tools."""
import asyncio
from contextlib import closing,redirect_stdout
from copy import deepcopy
import io,json,sqlite3,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch

from week13.src import case_paths,case_taskflow,case_cli
from week13.tests import test_case_generation_protocol_fix as protocol
from week13.tests.test_case_agent_requests import write

EVIDENCE=case_paths.ROOT/'results/generation-spec-json-20261021-01'
LEGACY_ERROR='generation must include exactly candidate spec and required original baseline_sysml_source'


def response(arguments):
    return dict(finish_reason='tool_calls',tool_calls=[dict(function=dict(name='proposal_prepare',arguments=arguments))])


class StrictGenerationArguments(unittest.TestCase):
    def test_object_and_one_spec_string_preserve_all_source_values(self):
        spec=dict(artifact=dict(modelica_source='model A\n  // literal \\n stays\nend A;',sysml_source='package A { attribute s : String = "quoted"; }'))
        for encoded in (False,True):
            with self.subTest(encoded=encoded):
                original=response(json.dumps(dict(spec=json.dumps(spec) if encoded else spec,baseline_sysml_source='package Original {\n}')))
                before=deepcopy(original)
                args,audit=case_taskflow.generation_arguments(original,needs_baseline=True)
                self.assertEqual(args,dict(spec=spec,baseline_sysml_source='package Original {\n}'))
                self.assertEqual(original,before)
                if encoded:
                    self.assertEqual(audit['additional_json_decodes'],1)
                    self.assertEqual(audit['source_response_sha256'],case_paths.sha_value(original))
                    self.assertTrue(audit['other_argument_values_unchanged'])
                else:self.assertIsNone(audit)
    def test_duplicate_nonfinite_trailing_multilevel_and_nonobjects_refused(self):
        invalid=[
            '{"spec":{},"spec":{}}',
            '{"spec":{"artifact":{"m":1,"m":2}}}',
            '{"spec":{"value":NaN}}',
            '{"spec":{"value":Infinity}}',
            '{"spec":{"value":1e999}}',
            '{"spec":{}}}',
            '{"spec":{}} {}',
            json.dumps(dict(spec='{"x":1,"x":2}')),
            json.dumps(dict(spec='{"value":NaN}')),
            json.dumps(dict(spec='{"value":1e999}')),
            json.dumps(dict(spec='{} trailing')),
            json.dumps(dict(spec=json.dumps('{}'))),
            json.dumps(dict(spec='[]')),
            json.dumps(dict(spec='null')),
            json.dumps(dict(spec=[])),
            json.dumps(dict(spec=42)),
            '[]',
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):case_taskflow.generation_arguments(response(arguments),needs_baseline=False)
    def test_wrapper_baseline_and_complete_single_call_required(self):
        for arguments in [dict(spec='{}'),dict(spec='{}',baseline_sysml_source=None),dict(spec='{}',baseline_sysml_source='original',checks=[]),dict(spec={'baseline_sysml_source':'misplaced'})]:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):case_taskflow.generation_arguments(response(json.dumps(arguments)),needs_baseline=True)
        with self.assertRaisesRegex(ValueError,'extra=baseline_sysml_source'):
            case_taskflow.generation_arguments(response(json.dumps(dict(spec={},baseline_sysml_source='extra'))),needs_baseline=False)
        for reason in ('length','unknown'):
            original=response('{"spec":{}}');original['finish_reason']=reason
            with self.assertRaisesRegex(ValueError,'complete single'):case_taskflow.generation_arguments(original,needs_baseline=False)
        original=response('{"spec":{}}');original['tool_calls']*=2
        with self.assertRaisesRegex(ValueError,'complete single'):case_taskflow.generation_arguments(original,needs_baseline=False)


class SavedSpecJson(unittest.TestCase):
    snapshot=protocol.GenerationProtocolFix.snapshot
    rewrite_response=protocol.GenerationProtocolFix.rewrite_response
    def setUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        self.evidence=patch.object(protocol,'EVIDENCE',EVIDENCE);self.evidence.start()
        protocol.GenerationProtocolFix.setUp(self)
    def tearDown(self):
        protocol.GenerationProtocolFix.tearDown(self);self.evidence.stop()
    def encoded_transport(self,ctx,payload):
        result=self.original_transport(ctx,payload)
        function=result['choices'][0]['message']['tool_calls'][0]['function']
        args=json.loads(function['arguments']);args['spec']=json.dumps(args['spec'],ensure_ascii=False)
        function['arguments']=json.dumps(args,ensure_ascii=False)
        return result
    def legacy_failure(self,repair=False):
        self.h.transport=self.encoded_transport
        with patch.object(self.flow,'_validate_generation',side_effect=ValueError(LEGACY_ERROR)):
            with self.assertRaisesRegex(ValueError,'generation must include'):asyncio.run(self.flow.generate())
            if repair:
                with self.assertRaisesRegex(ValueError,'generation must include'):asyncio.run(self.flow.repair('generate',1))
    def test_saved_attempt1_preserves_originals_ledger_and_no_source_projection(self):
        self.legacy_failure(repair=True);before=self.snapshot()
        ledger=Path(self.flow.ctx['runtime_authority']['ledger_path']);ledger_before=ledger.read_bytes()
        with patch.object(self.flow,'_runtime',side_effect=AssertionError('no HTTP')),patch.object(self.flow,'mcp',side_effect=AssertionError('no MCP')):
            result=self.flow.revalidate_generation(1)
        audit=case_paths.read_json(result['audit_path'])
        self.assertEqual(audit['projection'],'ONLY_SPEC_JSON_STRING_TO_OBJECT')
        self.assertFalse(audit['source_reference_projection']);self.assertEqual(audit['product_output_repair_added'],0)
        self.assertEqual(audit['source_response_sha256'],audit['spec_json_normalization']['source_response_sha256'])
        self.assertEqual(ledger.read_bytes(),ledger_before)
        for path,digest in before.items():self.assertEqual(case_paths.sha_file(self.root/path),digest,path)
        self.assertEqual(self.flow.revalidate_generation(1,expected_spec_digest=result['spec_digest']),result)
        self.assertEqual(self.flow._read_revalidated_generation()[0],result['spec'])
    def test_expected_digests_reject_before_any_new_receipt(self):
        self.legacy_failure();before=self.snapshot()
        for kwargs in (dict(expected_spec_digest='0'*64),dict(expected_baseline_digest='0'*64)):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError,'expected'):self.flow.revalidate_generation(**kwargs)
                self.assertEqual(self.snapshot(),before)
    def test_rejected_unknown_missing_candidate_or_ineligible_failure_refused(self):
        self.legacy_failure();ledger=self.flow.ctx['runtime_authority']['ledger_path']
        for status in ('unknown','rejected'):
            with closing(sqlite3.connect(ledger)) as db:
                with db:db.execute("UPDATE requests SET status=? WHERE category='generation'",(status,))
            with self.assertRaisesRegex(ValueError,'known complete'):self.flow.revalidate_generation()
        with closing(sqlite3.connect(ledger)) as db:
            with db:
                db.execute("UPDATE requests SET status='succeeded' WHERE category='generation'")
                db.execute("DELETE FROM counters WHERE kind='candidate'")
        with self.assertRaisesRegex(ValueError,'candidate event'):self.flow.revalidate_generation()
        failure_path=self.root/'failures/generate-0.json';failure=case_paths.read_json(failure_path);failure['error']='ValueError: arbitrary business failure';write(failure_path,failure)
        with self.assertRaisesRegex(ValueError,'not an eligible'):self.flow.revalidate_generation()
    def test_source_text_and_physics_are_never_repaired(self):
        self.legacy_failure()
        def change_source(args):
            spec=json.loads(args['spec']);spec['sources'][0]['text']='altered';args['spec']=json.dumps(spec)
        self.rewrite_response(change_source)
        with self.assertRaisesRegex(ValueError,'source text differs'):self.flow.revalidate_generation()
        self.assertFalse((self.root/'generation-revalidated.json').exists())
    def test_new_generation_uses_same_parser_and_saves_original_bound_normalization(self):
        self.h.transport=self.encoded_transport
        with patch.object(self.flow,'_prepare_generation',new=AsyncMock(return_value=dict(test_only=True))) as prepare:
            result=asyncio.run(self.flow.generate())
        self.assertTrue(result['test_only']);prepare.assert_awaited_once()
        original=case_paths.read_json(self.root/'turns/generate/response.json')
        self.assertIsInstance(json.loads(original['tool_calls'][0]['function']['arguments'])['spec'],str)
        audit=case_paths.read_json(self.root/'normalizations/generate-0-spec-json.json')
        self.assertEqual(audit['source_response_sha256'],case_paths.sha_value(original))
        self.assertFalse((self.root/'failures/generate-0.json').exists())
        self.assertFalse((self.root/'generation.json').exists())
    def test_tampered_audit_and_non_source_projection_mode_rejected(self):
        self.legacy_failure();saved=self.flow.revalidate_generation()
        audit=case_paths.read_json(saved['audit_path']);audit['source_reference_projection']=True;write(saved['audit_path'],audit)
        saved['audit_sha256']=case_paths.sha_value(audit);write(self.root/'generation-revalidated.json',saved)
        with self.assertRaisesRegex(ValueError,'audit binding'):self.flow._read_revalidated_generation()
    def test_cli_digest_guard_rejects_without_request_mutation(self):
        self.legacy_failure();before=self.snapshot();output=io.StringIO()
        with redirect_stdout(output):code=case_cli.main(['revalidate-generation','--request-id',self.rid,'--attempt','0','--expected-spec-digest','0'*64],flow_factory=lambda rid:self.flow)
        self.assertNotEqual(code,0);self.assertIn('expected spec digest',output.getvalue());self.assertEqual(self.snapshot(),before)


class OriginalBaselineSpecJson(unittest.TestCase):
    def test_sidecar_binds_original_response_not_in_memory_reencoding(self):
        from week13.tests import test_case_projects
        with patch.object(test_case_projects,'EVIDENCE',EVIDENCE):
            helper=test_case_projects.CaseProjects();helper.setUp()
            try:
                case=test_case_projects.case_projects.import_case(helper.inputs,'TEST_ONLY encoded spec original baseline')['case']
                ctx=helper.bootstrap(case,'TEST_ONLY_encoded_missing_sysml');helper.authorize(ctx['request_id']);flow=helper.flow(ctx['request_id'])
                planned=asyncio.run(flow.plan());flow.confirm(planned['displayed_digest'],'CONFIRM')
                transport=helper.transport
                def encoded(ctx,payload):
                    result=transport(ctx,payload);function=result['choices'][0]['message']['tool_calls'][0]['function']
                    args=json.loads(function['arguments']);args['spec']=json.dumps(args['spec'],ensure_ascii=False);function['arguments']=json.dumps(args,ensure_ascii=False)
                    return result
                helper.transport=encoded
                with patch.object(flow,'_validate_generation',side_effect=ValueError(LEGACY_ERROR)):
                    with self.assertRaisesRegex(ValueError,'generation must include'):asyncio.run(flow.generate())
                root=Path(ctx['root']);original_path=root/'turns/generate/response.json';original_bytes=original_path.read_bytes()
                result=flow.revalidate_generation();sidecar=case_paths.read_json(root/'baseline-sysml-completion.json')
                self.assertEqual(sidecar['source_response_sha256'],case_paths.sha_value(json.loads(original_bytes)))
                self.assertEqual(sidecar['source_response_path'],str(original_path))
                self.assertEqual(original_path.read_bytes(),original_bytes)
                self.assertEqual(sidecar['completed_baseline_spec']['artifact']['modelica_source'],flow.ctx['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'])
                self.assertEqual(flow._read_revalidated_generation()[0],result['spec'])
                self.assertTrue(case_taskflow.baseline_sysml_for_context(flow.ctx))
            finally:helper.tearDown()


if __name__=='__main__':unittest.main()
