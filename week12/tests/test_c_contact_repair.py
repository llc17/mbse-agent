import unittest
from unittest.mock import patch
from week12.src.integration import contact_repair_execution as r
from week12.src.integration.review import check_review_receipt


class ContactRepairChecks(unittest.TestCase):
    def test_identity_budget_and_stale_approval_rejected(self):
        packet,protocol=r.review_inputs()
        bad={'origin':'user_message','human_confirmed':True,'message_reference':'unit negative only','user_text':'synthetic, cannot execute',
             'scope':packet['scope'],'specification_digest':'aee33eb72f551dd60cd1652f48a93b054bc5f36444f74f04cd9db25bf833ded9'}
        with self.assertRaises(ValueError): check_review_receipt(bad,packet)
        self.assertEqual(protocol['budget_proposal']['maximum_scene_attempts'],54)
        self.assertEqual(protocol['budget_proposal']['scene_attempts_per_round'],2*len(r.CASE_ORDER))
        batch=object.__new__(r.Batch)
        batch.budget={'maximum_scene_attempts':54,'new_local_execution_checks_limit':18,'sysml_parse_limit':0,
                      'identity_probe_limit':0,'maximum_rounds':3,'execution_total_wall_s':1800}
        with patch.object(batch,'usage',return_value={'scene':54,'local':18,'sysml':0,'probe':0,'unresolved_processes':0,'process_seconds':0}):
            with self.assertRaises(RuntimeError): batch.reserve('scene','must not write',3)

    def test_contact_patch_preserves_constitutive_law_and_fixed_bindings(self):
        candidate=r.CANDIDATE.read_text(encoding='utf-8'); original=r.B_SOURCE.read_text(encoding='utf-8')
        self.assertIn('contactFace[i] = if faceActive[i] and fnFace[i] > 0 then 1 else 0;',candidate)
        self.assertIn('contactVertex[i] = if vertexActive[i] and fnVertex[i] > 0 then 1 else 0;',candidate)
        # Only diagnostic count ownership is new; compare all constitutive equations exactly.
        laws=candidate.replace('if faceActive[i] and fnFace[i] > 0 then','if fnFace[i] > 0 then').replace(
            'if vertexActive[i] and fnVertex[i] > 0 then','if fnVertex[i] > 0 then')
        # All force, slip, loss, accumulation and flow equations remain exact.
        for start,end in [('      deltaFace[i] =','      faceSelectionImproves[i] ='),
                          ('      deltaVertex[i] =','    vertexActive[1] ='),
                          ('    Fx = sum(','  end PolylineWheelContact;')]:
            self.assertEqual(original[original.index(start):original.index(end)],laws[laws.index(start):laws.index(end)])
        self.assertIn('not beforeEnd[i - 1] and not afterStart[i]',candidate)
        self.assertIn('faceActive[i] = (if vertexKind[i] == 1 then afterStart[i] else projection[i] >= 0) and\n                      beforeEnd[i]',candidate)
        self.assertNotIn('vertexNx[i]*nx[i - 1] + vertexNz[i]*nz[i - 1] >= 0',candidate)
        self.assertIn('axis-aligned terrain only',candidate)
        self.assertIn('model PolylineWheelContact = V11ContactC2.PolylineWheelContact;',r.isolated_b())
        for ident in r.CASE_ORDER:
            for refined in (False,True):
                script=r.build_script(ident,refined)
                self.assertLess(script.index('loadFile("V11ContactC2.mo")'),script.index('loadFile("V11PhysicalB1.mo")'))
                self.assertIn('numberOfIntervals=1000,',script) if ident.startswith('T') else None


if __name__=='__main__': unittest.main()
