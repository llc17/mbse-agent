"""Finite structured-status negation checks; saved reply only, zero runtime."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from week13.src.case_semantic_guard import protect_explanation

ROOT=Path(__file__).resolve().parents[1]
SAVED=ROOT/'results/updated-model-agent-experiments-20261031-01/E0/recovered-explanation.json'


class StructuredStatusNegation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=SAVED.read_bytes();cls.saved=json.loads(cls.raw)
        cls.binding=cls.saved['current_fact_binding'];cls.references=cls.saved['current_source_references']

    def check_text(self,text,displayed):
        before=deepcopy(self.binding)
        value=protect_explanation(text,self.binding,source_references=self.references)
        self.assertEqual(value['explanation_validation']['body_displayed'],displayed,text)
        self.assertEqual(value['raw_answer'],text)
        self.assertEqual(self.binding,before)
        self.assertFalse(value['explanation_validation']['full_semantic_validation'])
        if not displayed:self.assertIn('STRUCTURED_STATUS_CONTRADICTION',{x['code'] for x in value['explanation_validation']['issues']},text)
        return value

    def test_same_real_reply_is_redisplayed_without_changing_facts_or_original(self):
        value=self.check_text(self.saved['raw_answer'],True)
        self.assertEqual(value['explanation_validation']['status'],'NO_KNOWN_FACT_CONFLICT')
        self.assertEqual(value['explanation_validation']['facts_sha256'],self.saved['explanation_validation']['facts_sha256'])
        self.assertIn(self.saved['raw_answer'],value['answer'])
        self.assertEqual(SAVED.read_bytes(),self.raw)

    def test_explicit_negatives_cover_bare_and_qualified_fields(self):
        for marker in ('不代表','并非','不是'):
            for target in ('physical_status','contact_validity.physical_status','current_contact_review.contact_validity.physical_status','物理状态'):
                text=marker+' '+target+' 为 PASS。'
                with self.subTest(text=text):self.check_text(text,True)

    def test_short_chinese_status_target_does_not_negate_a_following_wrong_claim(self):
        for marker in ('不代表','并非','不是'):
            for separator in (' ','，','但','而是','；'):
                text=marker+'物理状态为PASS'+separator+'任务状态为失败。'
                with self.subTest(text=text):self.check_text(text,False)

    def test_qualified_target_does_not_negate_later_affirmative_conflicts(self):
        for text in ('不代表 contact_validity.physical_status 为 PASS 但 physical_status=PASS。',
                     '并非 contact_validity.physical_status 为 PASS；task_status=FAIL。',
                     '不是 physical_status=PASS validity_status=FAIL。',
                     '不代表 contact_validity.physical_status 为 PASS 本次task_status=FAIL。'):
            with self.subTest(text=text):self.check_text(text,False)

    def test_affirmative_physical_and_wrong_task_claims_still_fail(self):
        for text in ('physical_status=PASS。','contact_validity.physical_status 为 PASS。',
                     '物理状态为PASS。','task_status=FAIL。','任务状态为失败。','validity_status=FAIL。'):
            with self.subTest(text=text):self.check_text(text,False)

    def test_double_negatives_are_not_mistaken_for_a_negative_claim(self):
        for text in ('不是不代表 contact_validity.physical_status 为 PASS。',
                     '并非不是 physical_status=PASS。','不是不是物理状态为PASS。',
                     '不代表并非 contact_validity.physical_status 为 PASS。'):
            with self.subTest(text=text):self.check_text(text,False)
        self.check_text('不是并非不是 contact_validity.physical_status 为 PASS。',True)

    def test_true_later_status_and_affirmative_task_pass_are_preserved(self):
        for text in ('不是物理状态为PASS 任务状态为成功。',
                     '不代表 contact_validity.physical_status 为 PASS；physical_status=UNKNOWN。',
                     'task_status=PASS。','validity_status=PASS；physical_status=UNKNOWN。'):
            with self.subTest(text=text):self.check_text(text,True)


if __name__=='__main__':unittest.main()
