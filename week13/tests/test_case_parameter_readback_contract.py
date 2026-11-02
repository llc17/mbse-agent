"""TEST_ONLY explicit readback-name contract; mock runner/Popen, no launches."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET

from week13.src import case_toolchain as tool
from week13.src.case_scope import COMPILED_PARAMETER_NAMES, modelica_parameters


EVIDENCE = Path(__file__).resolve().parents[1] / 'results/parameter-readback-interface-20261026-01'
DEFAULT = tuple(COMPILED_PARAMETER_NAMES)


def synthetic_source(extra=()):
    declarations = ['parameter Real ' + name + '=' + str(index + 1) + ';'
                    for index, name in enumerate(DEFAULT)]
    declarations.extend('parameter Real ' + name + '=' + str(value) + ';' for name, value in extra)
    return 'model TEST_ONLY_Readback ' + ' '.join(declarations) + ' annotation(experiment(StopTime=2,Tolerance=.01,Interval=1)); end TEST_ONLY_Readback;'


class ParameterReadbackContract(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-', dir=EVIDENCE)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.popen = self.stack.enter_context(patch.object(tool.subprocess, 'Popen', side_effect=AssertionError('REAL_LAUNCH_FORBIDDEN')))
        self.stack.enter_context(patch.object(tool, 'emit_progress'))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def folder(self, label):
        path = self.root / label
        path.mkdir()
        return path

    def xml(self, path, source, *, omitted=(), overrides=None, calculated=()):
        root = ET.Element('fmiModelDescription')
        ET.SubElement(root, 'DefaultExperiment', tolerance='0.01')
        variables = ET.SubElement(root, 'ModelVariables')
        for name, value in modelica_parameters(source)[0].items():
            if name in omitted:
                continue
            node = ET.SubElement(variables, 'ScalarVariable', name=name,
                                 causality='calculatedParameter' if name in calculated else 'parameter')
            attributes = {} if name in calculated else dict(start=str((overrides or {}).get(name, value)))
            ET.SubElement(node, 'Real', **attributes)
        ET.ElementTree(root).write(path / 'model_init.xml', encoding='utf-8')

    def stdout(self, source, names, *, overrides=None):
        values = modelica_parameters(source)[0]
        return 'time=2,' + ','.join(name + '=' + str((overrides or {}).get(name, values[name])) for name in names) + '\n'

    def seed(self, label, source, names, *, output_names=None, xml_omitted=(), xml_overrides=None,
             runtime_overrides=None, calculated=()):
        path = self.folder(label)
        (path / 'model.mo').write_text(source, encoding='utf8')
        self.xml(path, source, omitted=xml_omitted, overrides=xml_overrides, calculated=calculated)
        stdout = self.stdout(source, names if output_names is None else output_names, overrides=runtime_overrides)
        command = tool._simulation_command(['TEST_ONLY.exe'], parameter_names=names)
        result = dict(stdout=stdout, returncode=0, timed_out=False, uncertain=False)
        tool._record_parameter_output(command, path, 'TEST_ONLY_ATTEMPT', 4242, 'TEST_ONLY_TASK', result, parameter_names=names)
        (path / 'simulation.stdout.log').write_text(stdout, encoding='utf8')
        return path

    def receipt(self, path, mutate):
        target = path / 'runtime-parameter-output.json'
        record = json.loads(target.read_text(encoding='utf8'))
        mutate(record)
        target.write_text(json.dumps(record), encoding='utf8')

    def mock_runner(self, source):
        """Exercise actual _run/command/receipt while Popen remains a mock."""
        commands = []
        def popen(command, **options):
            commands.append(list(command))
            names = command[-1].removeprefix('-output=').split(',')
            output = self.stdout(source, names).encode('utf8')
            return SimpleNamespace(pid=4242, returncode=0, communicate=lambda **_: (output, b''))
        self.popen.side_effect = popen
        self.stack.enter_context(patch.dict(tool.os.environ, {'V12_CASE_TASK_ID': 'TEST_ONLY_TASK'}))
        self.stack.enter_context(patch.object(tool, 'require_time'))
        self.stack.enter_context(patch.object(tool, 'remaining', return_value=60))
        self.stack.enter_context(patch.object(tool, 'authorization', return_value=dict(timeouts_s=dict(simulation=30))))
        self.stack.enter_context(patch.object(tool, 'process_attempt', return_value='TEST_ONLY_ATTEMPT'))
        self.stack.enter_context(patch.object(tool, 'mark_process_launched'))
        self.stack.enter_context(patch.object(tool, 'finish_process'))
        self.stack.enter_context(patch.object(tool._runner, '_finish', side_effect=lambda result, _: result))
        def execute(actual_source, model_name, out, stop, intervals, simulation_timeout_s):
            self.assertEqual(actual_source, source)
            path = Path(out)
            (path / 'model.mo').write_text(source, encoding='utf8')
            self.xml(path, source)
            result = tool._runner._run(['TEST_ONLY.exe'], path, simulation_timeout_s)
            (path / 'simulation.stdout.log').write_text(result['stdout'], encoding='utf8')
            return dict(compile=dict(status='TEST_ONLY'), simulation=result)
        backend = self.stack.enter_context(patch.object(tool._runner, 'execute_model', side_effect=execute))
        return backend, commands

    def test_default_exact_thirteen_and_old_schema_xml_stdout_receipt_compatibility(self):
        self.assertEqual(len(DEFAULT), 13)
        source = synthetic_source()
        path = self.seed('default', source, DEFAULT)
        record = json.loads((path / 'runtime-parameter-output.json').read_text(encoding='utf8'))
        self.assertEqual(record['schema'], 'v12-owned-runtime-parameter-output-1')
        self.assertEqual(record['requested_parameters'], list(DEFAULT))
        self.assertEqual(record['command'][-1], '-output=' + ','.join(DEFAULT))
        result = tool.parameter_readback(source, path)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(list(result['expected']), list(DEFAULT))
        self.assertEqual(set(result['runtime_final_values']), set(DEFAULT))
        self.popen.assert_not_called()

    def test_extra_parameter_flows_execute_command_receipt_xml_and_stdout(self):
        source = synthetic_source((('TEST_ONLY_extra', 29),))
        names = DEFAULT + ('TEST_ONLY_extra',)
        backend, commands = self.mock_runner(source)
        path = self.folder('extra')
        result = tool.execute_model(source, 'TEST_ONLY_Readback', path, 2, 2, parameter_names=names)
        self.assertEqual(backend.call_count, 1)
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(commands[0][-2:], ['-s=dassl', '-output=' + ','.join(names)])
        receipt = json.loads((path / 'runtime-parameter-output.json').read_text(encoding='utf8'))
        self.assertEqual(receipt['requested_parameters'], list(names))
        readback = result['runtime_parameter_readback']
        self.assertEqual(readback['status'], 'PASS', readback)
        self.assertEqual(readback['xml_initial_values']['TEST_ONLY_extra'], 29)
        self.assertEqual(readback['runtime_final_values']['TEST_ONLY_extra'], 29)
        self.assertIsNone(tool._execution_parameter_names.get())

    def test_source_missing_requested_parameter_rejects_before_runner_and_readback_unknown(self):
        source = synthetic_source()
        names = DEFAULT + ('TEST_ONLY_missing',)
        with patch.object(tool._runner, 'execute_model') as backend:
            with self.assertRaisesRegex(ValueError, 'absent from source'):
                tool.execute_model(source, 'TEST_ONLY_Readback', self.root, 2, parameter_names=names)
            backend.assert_not_called()
        result = tool.parameter_readback(source, self.root, parameter_names=names)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['missing_source_parameters'], ['TEST_ONLY_missing'])
        self.assertNotIn('TEST_ONLY_missing', result['actual'])
        self.popen.assert_not_called()

    def test_missing_stdout_or_xml_requested_evidence_cannot_pass(self):
        source = synthetic_source()
        for label, changes in (('missing-output', dict(output_names=DEFAULT[:-1])),
                               ('missing-xml', dict(xml_omitted=(DEFAULT[-1],)))):
            with self.subTest(label=label):
                result = tool.parameter_readback(source, self.seed(label, source, DEFAULT, **changes))
                self.assertEqual(result['status'], 'UNKNOWN', result)

    def test_source_xml_and_stdout_value_conflicts_fail(self):
        source = synthetic_source()
        for label, changes in (('bad-xml', dict(xml_overrides={DEFAULT[0]: 999})),
                               ('bad-stdout', dict(runtime_overrides={DEFAULT[0]: 999}))):
            with self.subTest(label=label):
                result = tool.parameter_readback(source, self.seed(label, source, DEFAULT, **changes))
                self.assertEqual(result['status'], 'FAIL', result)
                self.assertTrue(any('conflict' in issue for issue in result['issues']))

    def test_receipt_and_command_name_order_drift_do_not_pass(self):
        source = synthetic_source()
        for label, mutate in (
                ('receipt-order', lambda r: r.update(requested_parameters=list(reversed(DEFAULT)))),
                ('command-order', lambda r: r['command'].__setitem__(-1, '-output=' + ','.join(reversed(DEFAULT)))),
                ('receipt-extra', lambda r: r['requested_parameters'].append('TEST_ONLY_extra'))):
            with self.subTest(label=label):
                path = self.seed(label, source, DEFAULT)
                self.receipt(path, mutate)
                self.assertEqual(tool.parameter_readback(source, path)['status'], 'UNKNOWN')

    def test_bad_names_rejected_without_runner_or_launch(self):
        invalid = ([], (), '', 'TEST_ONLY_x', {'TEST_ONLY_x'}, {'name': 'TEST_ONLY_x'},
                   iter(('TEST_ONLY_x',)), [None], [1], ['a', 'a'], ['time'], ['x.y'],
                   ['x[1]'], ['a,b'], ['-override=x'], ['x y'], ['x\n'], ['\u8f6e'])
        with patch.object(tool._runner, 'execute_model') as backend:
            for names in invalid:
                with self.subTest(names=repr(names)), self.assertRaises(ValueError):
                    tool.execute_model(synthetic_source(), 'TEST_ONLY_Readback', self.root, 2, parameter_names=names)
            backend.assert_not_called()
        self.popen.assert_not_called()

    def test_command_override_or_existing_output_rejected(self):
        for flag in ('-output=x', '-output', '-override=x=1', '-override', '-overrideFile=x', '-overrideFile'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                tool._simulation_command(['TEST_ONLY.exe', flag])

    def test_execute_exception_restores_previous_context_without_replacing_runner_hook(self):
        outer = ('TEST_ONLY_outer',)
        token = tool._execution_parameter_names.set(outer)
        hook = tool._runner._run
        try:
            with patch.object(tool._runner, 'execute_model', side_effect=RuntimeError('TEST_ONLY failure')):
                with self.assertRaisesRegex(RuntimeError, 'TEST_ONLY failure'):
                    tool.execute_model(synthetic_source(), 'TEST_ONLY_Readback', self.root, 2)
            self.assertEqual(tool._execution_parameter_names.get(), outer)
            self.assertIs(tool._runner._run, hook)
        finally:
            tool._execution_parameter_names.reset(token)
        self.popen.assert_not_called()

    def test_sequential_explicit_then_default_names_never_leak(self):
        source = synthetic_source((('TEST_ONLY_extra', 29),))
        backend, commands = self.mock_runner(source)
        first = tool.execute_model(source, 'TEST_ONLY_Readback', self.folder('first'), 2, 2,
                                   parameter_names=('TEST_ONLY_extra',))
        second = tool.execute_model(source, 'TEST_ONLY_Readback', self.folder('second'), 2, 2)
        self.assertEqual(first['runtime_parameter_readback']['expected'], dict(TEST_ONLY_extra=29))
        self.assertEqual(tuple(second['runtime_parameter_readback']['expected']), DEFAULT)
        self.assertEqual([command[-1] for command in commands], ['-output=TEST_ONLY_extra', '-output=' + ','.join(DEFAULT)])
        self.assertEqual(backend.call_count, 2)
        self.assertIsNone(tool._execution_parameter_names.get())

    def test_list_input_is_frozen_and_helpers_explicit_names_do_not_inherit_context(self):
        names = ['TEST_ONLY_extra']
        immutable = tool._parameter_names(names)
        names.append('TEST_ONLY_new')
        self.assertEqual(immutable, ('TEST_ONLY_extra',))
        token = tool._execution_parameter_names.set(('TEST_ONLY_extra',))
        try:
            self.assertEqual(tool._simulation_command(['TEST_ONLY.exe'])[-1], '-output=' + ','.join(DEFAULT))
        finally:
            tool._execution_parameter_names.reset(token)

    def test_calculated_parameter_uses_runtime_value_not_xml_initial_guess(self):
        source = synthetic_source((('TEST_ONLY_base', 5), ('TEST_ONLY_derived', 'TEST_ONLY_base+2')))
        names = ('TEST_ONLY_derived', 'TEST_ONLY_base')
        path = self.seed('calculated', source, names, calculated=('TEST_ONLY_derived',))
        result = tool.parameter_readback(source, path, parameter_names=names)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['actual']['TEST_ONLY_derived'], 7)
        self.assertEqual(result['value_origins']['TEST_ONLY_derived'], 'SIMULATION_FINAL_STDOUT_EVALUATED_PARAMETER')

    def test_default_source_subset_no_longer_silently_passes_and_explicit_subset_can(self):
        source = 'model TEST_ONLY parameter Real a=1; end TEST_ONLY;'
        path = self.folder('subset')
        self.xml(path, source)
        self.assertEqual(tool.parameter_readback(source, path)['status'], 'UNKNOWN')
        self.assertEqual(tool.parameter_readback(source, path, parameter_names=('a',))['status'], 'PASS')

    def execution_summary(self, label, *, runtime_status, threshold=2, test_only=False, trajectory=False):
        from week13.src.inherited import model_artifact
        from week13.src import case_checks
        from week13.src.inherited import artifact_trajectory
        path = self.folder(label) / 'TEST_ONLY.csv'
        path.write_text('time,signal\n0,0\n2,3\n', encoding='utf8')
        spec = dict(artifact=dict(model_name='TEST_ONLY_Readback'), outputs=dict(signal='signal'),
                    checks=[dict(kind='final_ge', output='signal', threshold=threshold)])
        if trajectory:
            spec['trajectory_contract'] = dict(TEST_ONLY='MOCK_ASSESSMENT_ONLY')
        result = dict(compile=dict(status='PASS'), simulation=dict(status='PASS'),
                      runtime_parameter_readback=dict(status=runtime_status))
        if test_only:
            result['test_only'] = True
        mission = dict(task_status='PASS', validity_status='PASS', scope='TEST_ONLY_MOCK_NO_PHYSICS')
        with patch.object(case_checks, 'assess', return_value=mission), \
                patch.object(artifact_trajectory, 'evaluate_trajectory', return_value=mission):
            return model_artifact.summarize_execution(spec, result, path)

    def test_nonpass_parameter_evidence_survives_passing_signal_checks(self):
        for runtime_status in ('UNKNOWN', 'FAIL'):
            with self.subTest(runtime_status=runtime_status):
                result = self.execution_summary(runtime_status, runtime_status=runtime_status)
                self.assertEqual(result['task_status'], 'UNKNOWN', result)
                self.assertEqual(result['chain_status'], 'BLOCKED_PARAMETER_EVIDENCE')
                self.assertEqual(result['observations'][0]['status'], 'PASS')

    def test_parameter_pass_signal_pass_and_signal_fail_keep_expected_results(self):
        for threshold, expected in ((2, 'PASS'), (4, 'FAIL')):
            with self.subTest(expected=expected):
                result = self.execution_summary(expected, runtime_status='PASS', threshold=threshold)
                self.assertEqual(result['chain_status'], 'PASS')
                self.assertEqual(result['task_status'], expected)
                self.assertEqual(result['observations'][0]['status'], expected)

    def test_parameter_gate_trajectory_branch_and_existing_test_only_exemption(self):
        blocked = self.execution_summary('trajectory-blocked', runtime_status='UNKNOWN', trajectory=True)
        self.assertEqual(blocked['chain_status'], 'BLOCKED_PARAMETER_EVIDENCE')
        self.assertEqual(blocked['task_status'], 'UNKNOWN')
        self.assertEqual(blocked['trajectory_assessment']['task_status'], 'PASS')
        exempt = self.execution_summary('test-only', runtime_status='UNKNOWN', test_only=True)
        self.assertEqual(exempt['chain_status'], 'PASS')
        self.assertEqual(exempt['task_status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
