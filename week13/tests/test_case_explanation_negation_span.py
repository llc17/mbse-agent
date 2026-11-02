"""Approved finite offline negation regression; no product/runtime construction."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from week13.src.case_semantic_guard import explanation_facts,protect_explanation

ROOT=Path(__file__).resolve().parents[1]
FROZEN=ROOT/'results/agent-reliability-network-20261023-02/live/frozen-negation-replies.json'
FROZEN_SHA='c3052ef35855d96c162d630e22ec590451237178a0001356dc1f4c88fdfc7891'
FACTS_SHA='210f1792796a7bafdf7f89b4f32e74f6c60a3e730d5aa0a8f61253b013ee7b88'


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')).hexdigest()


def frozen_records():
    raw=FROZEN.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=FROZEN_SHA:raise AssertionError('frozen original drift')
    return json.loads(raw)['records']


def isolated_projection(saved):
    """Copy original raw/binding and restore only its saved factual projection.

    Runtime adds parameters while protecting replies. The saved binding lacks
    those parameters; recover them solely from the same saved validation facts.
    The saved raw binding has no v5 reviewed execution; recreate that row only
    inside this local projection from its original saved validation facts.
    No request, store, CaseBudget or runtime is constructed or updated.
    """
    binding=deepcopy(saved['binding']);facts=deepcopy(saved['explanation_validation']['facts'])
    # Preserve the historical hash assertion on the historical object. The
    # new program projection has additional trust-state fields and is checked
    # independently below; it must not be forced to equal an old report hash.
    if digest(facts)!=FACTS_SHA:raise AssertionError('frozen saved factual object drift')
    rows={row['version_id']:row for row in facts}
    for key in ('latest_request_execution','reviewed_execution'):
        record=binding.get(key)
        if record and record.get('version_id') in rows:
            row=rows[record['version_id']]
            if row.get('stair_dimensions'):record['parameters']=deepcopy(row['stair_dimensions'])
    if not binding.get('reviewed_execution') and len(facts)==2:
        row=facts[1]
        record={key:row[key] for key in ('version_id','task_id','project_id','tool_status','chain_status')}
        record['parameters']=deepcopy(row.get('stair_dimensions',{}))
        record['current_contact_review']={key:deepcopy(row[key]) for key in ('validity_status','task_status','completion_status','physical_status','hard_contradictions')}
        record['current_contact_review']['binding']=deepcopy(row['source_binding'])
        record['current_contact_review'].update(schema='v12-current-contact-review-1',status=row['validity_status'])
        record['current_contact_review']['binding'].update({key:row[key] for key in ('project_id','version_id','task_id')})
        binding['reviewed_execution']=record
    projected=explanation_facts(binding)
    originals={row['version_id']:row for row in facts}
    for row in projected:
        for key in ('task_status','validity_status','completion_status','physical_status','hard_contradictions','source_binding'):
            if row[key]!=originals[row['version_id']][key]:raise AssertionError('current isolated projection changed key fact: '+key)
    return binding,projected


class FiniteNegationSpan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records=frozen_records()
        cls.binding,cls.facts=isolated_projection(cls.records[0]['saved_result'])
        cls.current_facts_sha=digest(cls.facts)

    def checked(self,text,displayed,code=None):
        before=deepcopy(self.binding)
        # Generic assertions need a bound version heading: the real projection
        # contains two versions, and protect_explanation intentionally leaves
        # unscoped multi-version prose unattributed.
        text='# v000008\n'+text
        result=protect_explanation(text,self.binding);report=result['explanation_validation']
        self.assertEqual(report['body_displayed'],displayed,text)
        self.assertFalse(report['full_semantic_validation'])
        self.assertEqual(report['facts_sha256'],self.current_facts_sha)
        self.assertEqual(self.binding,before)
        self.assertEqual(result['raw_answer'],text)
        if code:self.assertIn(code,{issue['code'] for issue in report['issues']},text)
        return result

    def test_fixed_real_09_10_raw_replies_display_without_fact_changes(self):
        for record in self.records:
            with self.subTest(record=record['name']):
                saved=record['saved_result'];before=deepcopy(saved)
                binding,facts=isolated_projection(saved)
                result=protect_explanation(saved['raw_answer'],binding)
                report=result['explanation_validation']
                self.assertEqual(report['status'],'NO_KNOWN_FACT_CONFLICT')
                self.assertTrue(report['body_displayed'])
                self.assertEqual(report['facts'],facts)
                self.assertEqual(report['facts_sha256'],digest(facts))
                self.assertEqual(saved['explanation_validation']['facts_sha256'],FACTS_SHA)
                self.assertFalse(report['full_semantic_validation'])
                self.assertIn(saved['raw_answer'],result['answer'])
                self.assertEqual(saved,before)
        self.assertEqual(self.records[0]['saved_result']['explanation_validation']['status'],'KNOWN_FACT_CONFLICT')

    def test_long_equivalence_negations_only_cover_the_target(self):
        source=' state=succeeded、编译 PASS、仿真 PASS 或工具 PASS '
        for negative in ('不能','不得','不应'):
            for preposition in ('把','将'):
                for relation in ('当作','视为','等同于'):
                    text='也'+negative+preposition+source+relation+'车辆任务成功'
                    with self.subTest(text=text):self.checked(text,True)
        for text in ['不能说这台车能爬300mm楼梯。','任务未完成，工具通过不能当作任务通过。',
                     '不能把task_status=FAIL改写为INVALID_MODEL。','只解释参数继承与简化限制。']:
            with self.subTest(text=text):self.checked(text,True)

    def test_new_claim_after_negation_with_contrast_or_no_punctuation_is_blocked(self):
        base='不能把 state=succeeded、编译 PASS、仿真 PASS 或工具 PASS 当作车辆任务成功'
        tails=['，但本次任务成功','但本次任务成功','不过车辆任务已经通过','然而当前任务已经完成',
               '而是已经通过',' 本次任务成功','车辆任务已经通过',' task_status=PASS',
               '但 task_status=PASS','300mm通过']
        for tail in tails:
            with self.subTest(tail=tail):
                code='STRUCTURED_STATUS_CONTRADICTION' if 'task_status' in tail else 'UNSUPPORTED_TASK_SUCCESS'
                self.checked(base+tail,False,code)
        # A later independent negative statement still has its own negation.
        for tail in ['但本次不能说任务成功','本次不能说任务成功','；当前任务未完成']:
            with self.subTest(tail=tail):self.checked(base+tail,True)

    def test_double_negations_and_real_success_claims_remain_blocked(self):
        source=' state=succeeded、编译 PASS、仿真 PASS 或工具 PASS '
        bad=['本次任务成功。','这台车能爬300mm楼梯。','任务不是失败而是已经通过。',
             '不是不能把'+source+'当作车辆任务成功','并非不得将'+source+'视为任务成功',
             '不能不把'+source+'当作车辆任务成功','不应不将'+source+'等同于任务成功',
             '不能不承认任务成功','不得不承认任务成功','不是不能把工具PASS当作任务成功但本次任务成功']
        bad += ['不能不承认车辆任务成功','不能不承认本次任务已完成','并非不能把'+source+'当作车辆任务成功']
        for text in bad:
            with self.subTest(text=text):self.checked(text,False,'UNSUPPORTED_TASK_SUCCESS')

    def test_historical_cross_version_scope_and_new_assertions(self):
        good=['原错误回复声称本次任务已完成；当前任务FAIL。',
              '此前任务成功的说法属于旧版本；v000008仍task_status=FAIL。',
              '不能把历史工具PASS当作当前任务成功。']
        bad=['历史v000001任务成功；v000008本次任务成功。',
             'v000005 task_status=PASS。','v000008 physical_status=PASS。',
             '不能把历史工具PASS当作任务成功本次任务成功。',
             '原错误回复声称任务成功但当前任务已经通过。']
        for text in good+bad:
            with self.subTest(text=text):self.checked(text,text in good)
        self.checked('历史v000001任务成功；v000008任务未完成。',False,'UNBOUND_VERSION_REFERENCE')

    def test_task_fail_and_explicit_empty_hard_contradictions_still_protected(self):
        for text in ['v000008 task_status=FAIL表示INVALID_MODEL。','v000008任务FAIL意味着硬几何矛盾。']:
            with self.subTest(text=text):self.checked(text,False,'TASK_FAILURE_MISREPRESENTED_AS_INVALID_MODEL')
        for text in ['v000008 hard_contradictions缺少字段。','v000008 hard_contradictions不为空。']:
            with self.subTest(text=text):self.checked(text,False,'EXPLICIT_EMPTY_HARD_CONTRADICTIONS_MISREAD')
        self.checked('v000008 hard_contradictions=[]；有限检查未见硬矛盾；任务FAIL，物理UNKNOWN。',True)


if __name__=='__main__':unittest.main(verbosity=2)
