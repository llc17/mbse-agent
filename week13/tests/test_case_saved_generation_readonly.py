"""Read-only saved270 inspection guards; never mutate its original evidence."""
from copy import deepcopy
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from week13.src import case_paths,case_saved_generation as saved
from week13.src.case_taskflow import TaskFlow

EVIDENCE=case_paths.ROOT/'results/generation-protocol-repair-20261028-01'
ORIGIN='console_20261028T041658_ff29f5400a'

class SavedGenerationReadOnly(unittest.TestCase):
    def test_terminal_response_failure_and_authority_drift_refuse_before_writes(self):
        flow=TaskFlow.load(ORIGIN);reader=saved.read_json
        changes=[('process-exit.json',lambda value:value.update(local_process_terminated=False)),
                 ('rejected-response.json',lambda value:value.update(usage_known=False)),
                 ('generate-1.json',lambda value:value.update(error='other error')),
                 ('request.json',lambda value:value.update(authorization_sha256='0'*64))]
        for name,change in changes:
            with self.subTest(file=name),tempfile.TemporaryDirectory(prefix='TEST_ONLY_review_',dir=EVIDENCE) as temp:
                def changed(path):
                    value=deepcopy(reader(path))
                    if Path(path).name==name:change(value)
                    return value
                with patch.object(saved,'read_json',side_effect=changed),self.assertRaises(ValueError):saved.inspect_saved_generation(flow,1,temp)
                self.assertEqual(list(Path(temp).iterdir()),[])
    def test_expected_digest_rejects_after_full_checks_without_export_or_original_writes(self):
        flow=TaskFlow.load(ORIGIN)
        with tempfile.TemporaryDirectory(prefix='TEST_ONLY_expected_',dir=EVIDENCE) as temp:
            with self.assertRaisesRegex(ValueError,'expected candidate digest'):
                saved.inspect_saved_generation(flow,1,temp,expected_spec_digest='0'*64)
            self.assertEqual(list(Path(temp).iterdir()),[])
    def test_output_must_be_new_results_and_never_original_request(self):
        flow=TaskFlow.load(ORIGIN)
        with self.assertRaises(PermissionError):saved.inspect_saved_generation(flow,1,flow.root/'new-review')
    def test_sysml_prompt_example_is_legal_parser_input(self):
        from week13.src.case_backend import module
        from week13.src.case_toolchain import check_sysml
        prompt=module('artifact_sysml').CONTRACT_PROMPT
        example=prompt.split('`package Foo ',1)[1].split('`',1)[0]
        result=check_sysml('package Foo '+example)
        self.assertEqual(result['status'],'PASS',result.get('issues'))
        self.assertIn('never use Modelica-style `end package;`',prompt)

if __name__=='__main__':unittest.main()
