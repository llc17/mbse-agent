"""Known correction guard fixtures only; no live budget or model call."""
from copy import deepcopy
import json,unittest
from unittest.mock import patch
from week13.tests import test_case_http_rejection_guard as first
from week13.src.case_budget import connection
from week13.src.case_paths import sha_value
from week13.src.case_scope import STAIR_SCOPE_PACKET


class CorrectionGuard(unittest.TestCase):
    setUp=first.RejectionGuard.setUp
    tearDown=first.RejectionGuard.tearDown
    authorize=first.RejectionGuard.authorize
    def prepared(self):
        self.authorize();self.b.reserve_http(self.new,self.digest,'design',self.b.authority['model_controls']);self.b.launch_http(self.new)
        self.b.finish_http(self.new,error='HTTPStatusError',uncertain=True)
        self.b.authority['context_bindings']={'fixture':'fixture_digest'}
        self.corrected=self.old+'_empty_object_correction_1';self.corrected_hash='corrected_payload'
        self.packet=dict(scope=deepcopy(STAIR_SCOPE_PACKET),support='SUPPORTED',intent='CHANGE',missing=[],unsupported=[])
        self.parameters=dict(m=85,I_pitch=20,T_main_max=60,T_nose_max=25,stepH=.18,stepD=.26)
        proof=dict(schema='v12-http-empty-object-correction-proof-1',authority_sha256='fixture_authority',request_context_id='fixture',request_digest='fixture_digest',
            ledger_path=str(self.b.path.resolve()),correction_request_id=self.corrected,new_payload_sha256=self.corrected_hash,
            preserved_unknown_rows={rid:sha_value(self.b.request(rid)) for rid in (self.old,self.new)},
            original_freeze=self.proof['original_freeze'],approved_context_bindings=deepcopy(self.b.authority['context_bindings']),
            evidence_hashes=self.proof['evidence_hashes'],origin_request_root=str(self.root),baseline_ref={'version_id':'v000005'},expected_parameters=self.parameters,expected_scope=STAIR_SCOPE_PACKET)
        self.b.authorize_http_schema_correction(self.corrected,self.corrected_hash,proof)
        return proof
    def succeed(self):
        self.b.reserve_http(self.corrected,self.corrected_hash,'design',self.b.authority['model_controls']);self.b.launch_http(self.corrected)
        self.b.finish_http(self.corrected,response={'fixture':'COMPLETE'},tokens=10,output=5,cost=.001)
    def test_ordinary_work_requires_complete_corrected_response_and_exact_saved_valid_plan(self):
        proof=self.prepared()
        with connection(self.b.path) as db:
            with self.assertRaises(RuntimeError):self.b._blocked(db,'case_generate_fixture')
        self.succeed()
        current={'baseline_ref':proof['baseline_ref'],'plan':self.packet}
        with patch('week13.src.case_budget.load_request',return_value=current):
            with connection(self.b.path) as db:
                with self.assertRaises(RuntimeError):self.b._blocked(db,'case_generate_fixture')
            validation=dict(packet_digest=sha_value(self.packet),executable=True,after=self.parameters)
            (self.root/'plan-validation.json').write_text(json.dumps(validation),encoding='utf8')
            with connection(self.b.path) as db:self.b._blocked(db,'case_generate_fixture')
            validation['after']=dict(self.parameters,stepH=.19)
            (self.root/'plan-validation.json').write_text(json.dumps(validation),encoding='utf8')
            with connection(self.b.path) as db:
                with self.assertRaises(RuntimeError):self.b._blocked(db,'case_generate_fixture')
        self.assertEqual(self.b.request(self.old),self.original)
        with connection(self.b.path) as db:self.assertEqual(db.execute("SELECT count(*) FROM counters WHERE kind='software_repair'").fetchone()[0],1)
    def test_wrong_payload_new_unknown_or_over_reservation_remains_blocked(self):
        proof=self.prepared()
        with self.assertRaises(ValueError):self.b.reserve_http(self.corrected,'different','design',self.b.authority['model_controls'])
        self.succeed()
        current={'baseline_ref':proof['baseline_ref'],'plan':self.packet}
        (self.root/'plan-validation.json').write_text(json.dumps(dict(packet_digest=sha_value(self.packet),executable=True,after=self.parameters)),encoding='utf8')
        with patch('week13.src.case_budget.load_request',return_value=current):
            with connection(self.b.path) as db:
                db.execute('UPDATE requests SET actual_tokens=reserve_tokens+1 WHERE request_id=?',(self.corrected,))
                with self.assertRaises(RuntimeError):self.b._blocked(db,'case_generate_fixture')
        self.assertEqual(self.b.request(self.old),self.original)


if __name__=='__main__':unittest.main()
