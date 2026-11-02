"""Narrow regression from the two real 2026-10-23 replies; zero product HTTP."""
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_taskflow as tf
from week13.src.case_paths import ROOT,read_json,sha_value
from week13.src.case_semantic_guard import plan_semantic_validation

RID='agent85_300_260_network_20261023_02'
RESULT=ROOT/'results/agent-reliability-network-20261023-02'
REQUEST=ROOT/'runs/case-projects/case_3b0265f496f84ae9b800/requests'/RID

def packet(attempt):
    stage='plan' if attempt==0 else 'plan_repair_'+str(attempt)
    return tf.parse_tool(read_json(REQUEST/'turns'/stage/'response.json'),'case_request_plan','packet')

class FixedDateTime(datetime):
    @classmethod
    def now(cls,tz=None):
        value=cls(2026,10,23,6,30,tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)

class RealReplySemanticRegression(unittest.TestCase):
    def test_two_original_replies_are_not_current_frozen_edits(self):
        baseline=read_json(REQUEST/'baseline-snapshot.json')['frozen_spec']
        for attempt in (0,1):
            with self.subTest(attempt=attempt):
                value=packet(attempt);before=deepcopy(value)
                self.assertEqual(plan_semantic_validation(value,baseline)['status'],'NO_KNOWN_FACT_CONFLICT')
                self.assertEqual(value,before)

    def test_length_units_and_mass_parameter_boundaries(self):
        good=['本次将stepH从0.15 m改为0.30 m。','本次将stepH从0.15m改为0.30m。',
              '本次将stepH从.15 m改为.30 m。','本次将stepH从1.5e-1m改为3e-1m。',
              '本次将stepH从1.5e-1 m改为3e-1 m。','本次将stepH由0.15 m调整为0.30 m。']
        bad=['本次将m从85改为90。','质量 m 由85kg调到90kg。','本次修改车辆质量。','本次调整质量m从85改为90。']
        self.check_claims(good,bad)

    def test_historical_facts_are_local_and_current_edits_remain_rejected(self):
        good=['本次属于范围B的楼梯尺寸参数改型：所选v000005基线已含此前已审接触与FtC修复。',
              '本次方案中基线已含此前已审FtC修复。','基线已含此前已审FtC修复。',
              '本次继承基线已修复的FtC分母保护。','本次不修改接触方程。']
        bad=['基线已含旧修复，本次再次修改FtC。','基线已含旧修复本次再次修改FtC。',
             '本次继承旧修复但再次修改FtC。','本次修复FtC分母保护。',
             '本次唯一改动是修复FtC分母。','本次唯一改动是此前已审的FtC修复。',
             '不调整质量但修复接触方程。']
        self.check_claims(good,bad)

    def test_prior_observed_current_only_change_bug_is_still_blocked(self):
        old=ROOT/'results/shallow-plan-protocol-20261022-03/action-flat85_180_260_20261022_03-plan.json'
        value=read_json(old)['result']['plan']
        self.assertTrue(plan_semantic_validation(value,{})['issues'])

    def check_claims(self,good,bad):
        for claim in good+bad:
            value=dict(intent='CHANGE',support='SUPPORTED',scope=deepcopy(tf.STAIR_SCOPE_PACKET),
                design=dict(summary=claim,assumptions=[]),rationale='',defaults=[],unknowns=[])
            with self.subTest(claim=claim):
                self.assertEqual(bool(plan_semantic_validation(value,{})['issues']),claim in bad)

class CompleteSemanticEvidenceRegression(unittest.TestCase):
    def setUp(self):
        self.ctx=read_json(RESULT/'live/local-repair-fixture-context.json')
        self.flow=tf.TaskFlow(deepcopy(self.ctx))
        self.flow.refresh=lambda:self.flow.ctx
        self.addCleanup(patch.stopall)
        patch('week13.src.case_requests.require_planning',lambda rid:deepcopy(self.ctx)).start()
        patch.object(tf,'datetime',FixedDateTime).start()
        patch('week13.src.case_budget.CaseBudget.__init__',side_effect=AssertionError('no CaseBudget construction')).start()

    def evidence(self):return self.flow._plan_normalization_evidence(1,semantic_only=True)

    def test_complete_successful_original_reply_revalidates_without_transform(self):
        value,audit=self.evidence()
        self.assertEqual(value,packet(1))
        self.assertEqual(audit['field_changes'],{})
        self.assertFalse(audit['business_values_changed'])
        self.assertEqual(audit['source_packet_sha256'],sha_value(value))
        self.assertEqual(audit['product_http_added'],0)

    def test_wire_protocol_exit_and_failure_drift_are_rejected(self):
        cases=[('request.json','payload'),('protocol-response.json','usage'),('process-exit.json','exit'),('plan-1.json','failure')]
        for name,kind in cases:
            def changed(path,kind=kind,name=name):
                value=read_json(path)
                if Path(path).name!=name or (name=='request.json' and Path(path).parent.name=='plan_repair_1'):return value
                value=deepcopy(value)
                if kind=='payload':value['payload']['max_tokens']=1
                elif kind=='usage':value['usage']['total_tokens']+=1
                elif kind=='exit':value['returncode']=1
                else:value['error']='ValueError: unrelated failure'
                return value
            with self.subTest(kind=kind),patch.object(tf,'read_json',side_effect=changed):
                with self.assertRaises(ValueError):self.evidence()

    def test_UNKNOWN_ledger_is_not_a_semantic_recovery(self):
        connect=sqlite3.connect
        with tempfile.TemporaryDirectory(dir=RESULT) as directory:
            target=Path(directory)/'ledger.sqlite'
            original=connect(Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)
            fixture=connect(target);original.backup(fixture);original.close()
            with fixture:fixture.execute("UPDATE requests SET status='unknown' WHERE request_id=?",('case_plan_repair_1_'+RID,))
            fixture.close()
            ledger_uri=Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro'
            with patch.object(tf.sqlite3,'connect',side_effect=lambda *a,**kw:connect(target) if a[0]==ledger_uri else connect(*a,**kw)):
                with self.assertRaisesRegex(ValueError,'no UNKNOWN/closed recovery'):self.evidence()

    def test_closed_or_retired_ledger_cannot_revalidate(self):
        connect=sqlite3.connect
        ledger_uri=Path(self.ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro'
        for key in ('closed','retired'):
            with self.subTest(key=key),tempfile.TemporaryDirectory(dir=RESULT) as directory:
                target=Path(directory)/'ledger.sqlite'
                original=connect(ledger_uri,uri=True);fixture=connect(target)
                original.backup(fixture);original.close()
                with fixture:fixture.execute('INSERT INTO meta VALUES(?,?)',(key,'true'))
                fixture.close()
                with patch.object(tf.sqlite3,'connect',side_effect=lambda *a,**kw:connect(target) if a[0]==ledger_uri else connect(*a,**kw)):
                    with self.assertRaisesRegex(ValueError,'no UNKNOWN/closed recovery'):self.evidence()

if __name__=='__main__':unittest.main(verbosity=2)
