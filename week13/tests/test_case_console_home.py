"""Explicit console entry and selection memory; no backend execution or HTTP."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from week13.src.case_console import ActionError,Console,write

EVIDENCE=Path(__file__).resolve().parents[1]/'results/console-home-fix-20261028-01/test-tmp/home'


class Runner:
    def __init__(self):
        self.reads=[]
        self.rows=[dict(case_id='case_A',project_id='project_A',name='案例 A'),dict(case_id='case_B',project_id='project_B',name='案例 B')]
        self.contexts={
            'request_'+letter:dict(request_id='request_'+letter,project_id='project_'+letter,baseline_version_id='v000000',raw_request='TEST_ONLY需求'+letter,stage='DRAFT',plan=None,budget=None)
            for letter in ('A','B')}

    def data(self,action,**options):
        self.reads.append((action,options))
        if action=='context':return deepcopy(self.contexts[options['request_id']])
        if action=='discover':
            target=options.get('case_id') or 'case_A'
            row=next(row for row in self.rows if target in (row['case_id'],row['project_id']))
            baseline=dict(version_id='v000000',parameters=dict(m=85),selectable=True)
            return dict(row,project_label=row['name'],baselines=[baseline],default_baseline=baseline,controls={},requests=[])
        raise AssertionError(action)

    def projects(self,action,**options):
        self.reads.append(('projects-'+action,options))
        if action=='list':return dict(cases=deepcopy(self.rows))
        if action=='inspect':
            if options['path']=='invalid':raise ActionError('TEST_ONLY非法资料路径')
            return dict(files=[],model_candidates=[dict(path='a.mo',model_name='A',support=dict(status='SUPPORTED')),dict(path='b.mo',model_name='B',support=dict(status='SUPPORTED'))])
        raise AssertionError('unexpected mutation '+action)

    def call(self,*args,**kwargs):raise AssertionError('no request/HTTP/execution allowed')


def stored():
    return dict(schema_version=2,case_id='case_A',cases={
        'case_'+letter:dict(request_id='request_'+letter,baseline_version_id='v000000',baseline_explicit=True,interrupted_requests=['request_'+letter])
        for letter in ('A','B')})


class ConsoleHomeTests(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=EVIDENCE)
        self.root=Path(self.temp.name);self.runner=Runner();self.out=[]

    def tearDown(self):self.temp.cleanup()

    def ui(self):return Console(self.runner,console_root=self.root,output=self.out.append,input_fn=lambda _: '')

    def test_new_and_existing_start_help_quit_have_zero_backend_and_no_selection_write(self):
        for history in (None,stored()):
            with self.subTest(history=bool(history)):
                if history:write(self.root/'selection.json',history)
                before=(self.root/'selection.json').read_bytes() if history else None
                ui=self.ui();ui.start();self.assertEqual(ui.mode(),'首页')
                self.assertIsNone(ui.catalog);self.assertIsNone(ui.ctx);self.assertIsNone(ui.selection['request_id'])
                ui.handle('/help');ui.handle('/quit')
                self.assertEqual(self.runner.reads,[])
                if history:self.assertEqual((self.root/'selection.json').read_bytes(),before)
                else:self.assertFalse((self.root/'selection.json').exists())

    def test_home_input_never_implicitly_creates_requirement_budget_or_call(self):
        ui=self.ui();ui.start()
        for text in ('/new 改重量','中文需求','/budget','/plan','/plan brief','确认执行','/result','/result full','/status','/ask 问题','/sessions','/baseline'):
            ui.handle(text)
        self.assertEqual(self.runner.reads,[])
        self.assertFalse((self.root/'selection.json').exists());self.assertIsNone(ui.ctx)
        self.assertIn('请先选择案例','\n'.join(self.out));self.assertIn('当前在首页','\n'.join(self.out))

    def test_home_list_does_not_mark_previous_case_current(self):
        write(self.root/'selection.json',stored());before=(self.root/'selection.json').read_bytes()
        ui=self.ui();ui.start();ui.handle('/case list')
        self.assertNotIn('[当前]','\n'.join(self.out));self.assertIsNone(ui.catalog)
        self.assertEqual((self.root/'selection.json').read_bytes(),before)
        self.assertEqual([row[0] for row in self.runner.reads],['projects-list'])

    def test_resume_restores_only_saved_case_request_and_baseline(self):
        write(self.root/'selection.json',stored());ui=self.ui();ui.start();ui.handle('/resume')
        self.assertEqual(ui.catalog['case_id'],'case_A');self.assertEqual(ui.ctx['request_id'],'request_A')
        self.assertEqual(ui.selection['baseline_version_id'],'v000000')
        self.assertEqual(ui.selection['interrupted_requests'],['request_A'])
        self.assertEqual([row[0] for row in self.runner.reads],['discover','context'])

    def test_empty_resume_and_flat_history_start_are_readonly_until_resume(self):
        ui=self.ui();ui.start();ui.handle('/resume');self.assertEqual(self.runner.reads,[])
        self.assertIn('尚无可恢复','\n'.join(self.out))
        old=dict(request_id='request_A',baseline_version_id='v000000',baseline_explicit=True,interrupted_requests=['request_A'])
        write(self.root/'selection.json',old);before=(self.root/'selection.json').read_bytes()
        ui=self.ui();ui.start();self.assertEqual((self.root/'selection.json').read_bytes(),before)
        ui.handle('/resume');self.assertEqual(ui.ctx['request_id'],'request_A')
        self.assertEqual(ui.state['cases']['case_A']['interrupted_requests'],['request_A'])

    def test_home_open_resume_a_b_a_and_cold_restart_preserve_per_case_memory(self):
        write(self.root/'selection.json',stored());ui=self.ui();ui.start()
        ui.handle('/case open "案例 A"');self.assertEqual(ui.ctx['request_id'],'request_A')
        ui.handle('/home');self.assertEqual(ui.mode(),'首页');self.assertIsNone(ui.ctx)
        ui.handle('/case open case_B');self.assertEqual(ui.ctx['request_id'],'request_B')
        ui.handle('/case open case_A');self.assertEqual(ui.ctx['request_id'],'request_A')
        ui.handle('/home');saved=(self.root/'selection.json').read_bytes();ui.handle('/quit')
        cold=self.ui();cold.start();self.assertIsNone(cold.catalog);self.assertEqual(cold.mode(),'首页')
        self.assertEqual((self.root/'selection.json').read_bytes(),saved)
        cold.handle('/resume');self.assertEqual(cold.ctx['request_id'],'request_A')
        self.assertEqual(cold.state['cases']['case_B']['request_id'],'request_B')

    def test_open_request_from_home_selects_its_owning_case(self):
        ui=self.ui();ui.start();ui.handle('/open request_B')
        self.assertEqual(ui.catalog['case_id'],'case_B');self.assertEqual(ui.ctx['request_id'],'request_B')

    def test_cancelled_or_invalid_import_keeps_home_and_original_memory(self):
        write(self.root/'selection.json',stored());before=(self.root/'selection.json').read_bytes()
        ui=self.ui();ui.start();ui.handle('/case import')
        self.assertEqual(self.runner.reads,[])
        with self.assertRaises(ActionError):ui.handle('/case import invalid')
        ui.handle('/case import valid') # model choice empty cancels
        self.assertIsNone(ui.catalog);self.assertEqual(ui.mode(),'首页')
        self.assertEqual((self.root/'selection.json').read_bytes(),before)


if __name__=='__main__':unittest.main()
