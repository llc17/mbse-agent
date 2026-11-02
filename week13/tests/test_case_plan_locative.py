"""Preposed location versus edited object; no product or tool execution."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from week13.src.case_paths import ROOT,read_json,sha_file,sha_value
from week13.src.case_semantic_guard import plan_semantic_validation
from week13.tests.test_case_agent_flow_repair import packet

OUT=ROOT/'results/agent-flow-repair-20261025-01/implementation/local-repair-2'


class PreposedLocation(unittest.TestCase):
    def test_real_first_failure_is_readonly_replayed_not_revalidated(self):
        failure=read_json(OUT/'originals/failures/plan-0.json')
        original=deepcopy(failure)
        response=failure['response'];p=json.loads(response['tool_calls'][0]['function']['arguments'])['packet']
        baseline=read_json(OUT/'originals/baseline-snapshot.json')['frozen_spec']
        namespace={};exec(compile((OUT/'case_semantic_guard.py.before').read_text(encoding='utf8'),'FROZEN_BEFORE_LOCAL_REPAIR_2','exec'),namespace)
        old=namespace['plan_semantic_validation'](p,baseline)
        self.assertEqual([(x['field'],x['target']) for x in old['issues']],[('defaults[1]','重心')])
        current=plan_semantic_validation(p,baseline)
        self.assertFalse(current['issues'])
        self.assertFalse(current['full_semantic_validation'])
        self.assertEqual(failure,original)
        self.assertIn('known plan semantic conflict',failure['error'])
        self.assertEqual(sha_value(p),sha_value(json.loads(response['tool_calls'][0]['function']['arguments'])['packet']))
        for row in read_json(OUT/'before-manifest.json')[1:]:
            self.assertEqual(sha_file(row['source']),row['sha256'])
            self.assertEqual(sha_file(row['copy']),row['copy_sha256'])

    def test_locative_relation_does_not_depend_on_mass_number_or_prefix_topic(self):
        for amount in (0,3.375,61,0.02):
            for place in ('在原重心处','于原参考重心位置','在参考重心','于重心处'):
                claims=[
                    f'点质量与惯量按理想点质量近似处理：仅{place}考虑新增质量（本次新增量为 {amount} kg），I_pitch保持不变。',
                    f'惯量继承：本次{place}增加{amount}千克载荷。',
                ]
                for claim in claims:
                    p=packet();p['defaults']=[claim]
                    with self.subTest(amount=amount,place=place,claim=claim):self.assertFalse(plan_semantic_validation(p,{})['issues'])

    def test_location_does_not_exempt_a_later_frozen_object_or_action(self):
        bad=[
            '点质量与惯量按近似处理：在原重心处新增质量，本次将I_pitch改为25。',
            '本次在原重心处增加惯量。',
            '本次于原重心处新增接触模型。',
            '在原重心处新增质量并改变惯量。',
            '在原重心处新增质量后将重心移动到前轴。',
            '于原参考重心位置增加载荷但本次延长StopTime。',
            '本次在I_pitch上增加3。',
            '本次增加重心偏移。',
            '本次将重心处的质量移到前轴。',
        ]
        for claim in bad:
            for field in ('summary','defaults','unknowns'):
                p=packet()
                if field=='summary':p['design']['summary']=claim
                else:p[field]=[claim]
                with self.subTest(claim=claim,field=field):self.assertTrue(plan_semantic_validation(p,{})['issues'])


if __name__=='__main__':unittest.main()
