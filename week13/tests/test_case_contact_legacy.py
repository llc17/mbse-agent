"""Local legacy entry compatibility: no product transport or model process."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from week13.src import case_cli,case_session
from week13.src.case_contact_review import with_review
from week13.tests.test_case_contact_integration import OUT,write_fixture


class LegacyContactTests(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='integration-legacy-',dir=OUT)
        self.root=Path(self.tmp.name);self.snapshot,self.base=write_fixture(self.root)
        (self.root/'run.json').write_text(json.dumps(dict(history_success_scope='TEST_ONLY 原历史成功原文')),encoding='utf8')
        snapshot=self.snapshot;root=self.root
        class Service:
            project_root=root
            def versions_list(self):return dict(versions=dict(items=[snapshot]))
            def _current_project(self):return dict(project_id=snapshot['project_id'])
        self.service=Service()
    def tearDown(self):self.tmp.cleanup()
    def status(self):
        with patch.object(case_session,'RUN',self.root):return case_session.status(self.service)

    def test_legacy_status_reviews_one_candidate_and_retains_history(self):
        original=deepcopy(self.snapshot)
        with patch('week13.src.case_contact_review.with_review',wraps=with_review) as checked:status=self.status()
        self.assertEqual(checked.call_count,1)
        self.assertEqual(status['latest_execution']['task_status'],'UNKNOWN')
        self.assertEqual(status['latest_execution']['current_contact_review']['status'],'FAIL')
        self.assertIsNone(status['last_task_pass_snapshot'])
        self.assertEqual(status['historical_last_task_pass_record']['task_status'],'PASS')
        self.assertEqual(status['historical_case_success'],'TEST_ONLY 原历史成功原文')
        self.assertEqual(self.snapshot,original)

    def test_legacy_status_missing_evidence_unknown_does_not_seek_another_csv(self):
        (self.base/'tool/model_res.csv').unlink()
        with patch('week13.src.case_contact_review.with_review',wraps=with_review) as checked:status=self.status()
        self.assertEqual(checked.call_count,1)
        self.assertEqual(status['latest_execution']['current_contact_review']['status'],'UNKNOWN')
        self.assertEqual(status['latest_execution']['task_status'],'UNKNOWN')
        self.assertIsNone(status['last_task_pass_snapshot'])

    def test_legacy_ask_reopen_context_and_fixed_conclusion_override_false_product_text(self):
        before=self.status();answer='是，300mm通过。（刻意注入错误文本）'
        original_assistant=dict(role='assistant',content='TEST_ONLY 历史解释原文：通过。',reasoning_content='TEST_ONLY 保留原协议')
        history=[dict(question='TEST_ONLY 历史问题',assistant_message=original_assistant)]
        path=self.root/'dialogue.json';path.write_text(json.dumps(history,ensure_ascii=False),encoding='utf8');old=path.read_bytes()
        captured=[];writes=[]
        async def fake_product(rid,messages,tools):
            self.assertEqual(tools,[]);captured.append(deepcopy(messages))
            return dict(content=answer,assistant_message=dict(role='assistant',content=answer))
        with patch.object(case_cli,'RUN',self.root),patch.object(case_cli,'require_time'),patch.object(case_cli,'compare',return_value=dict(status=before)),patch.object(case_cli,'status',return_value=before),patch.object(case_cli,'factual_notes',return_value={}),patch.object(case_cli,'_product_call',side_effect=fake_product),patch.object(case_cli,'runtime',return_value=SimpleNamespace(usage=lambda:dict(test_only=True))),patch.object(case_cli,'save_json',side_effect=lambda *args,**kwargs:writes.append((args,kwargs))):
            for reopen in (False,True):
                result=asyncio.run(case_cli.ask('TEST_ONLY 现在是否可信？',reopen=reopen))
                self.assertIn('INVALID_MODEL',result['system_conclusion'])
                self.assertEqual(result['answer'],answer,'不得伪称产品文本已被自动纠正')
                self.assertEqual(result['current_contact_review']['status'],'FAIL')
        self.assertEqual(path.read_bytes(),old)
        for messages in captured:
            self.assertIn('当前只读接触复核优先',messages[0]['content'])
            self.assertEqual(json.loads(messages[1]['content'])['current_contact_review']['status'],'FAIL')
            self.assertIn('历史对话，不代表当前接触复核',messages[2]['content'])
            self.assertEqual(messages[3],original_assistant)
            self.assertIn('覆盖历史解释',messages[-2]['content'])
        self.assertEqual(len(writes),2,'仅注入保存新问答，不操作任何真实DB/预算')


if __name__=='__main__':unittest.main()
