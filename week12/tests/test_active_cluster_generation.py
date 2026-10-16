"""Bound graph/generation tests. Real Modelica and SysML execution belongs to the root ledger."""
import hashlib
import json
import tempfile
import unittest
import re
from pathlib import Path
from week12.src.active_cluster.design import ROOT, APPROVED_SHA, UnsupportedDesign, build_design
from week12.src.active_cluster.modelica import lower, flight_plan, _geometry
from week12.src.active_cluster.generator import _sysml,generate

SPEC=ROOT/"week12/results/s3-c-open-configuration-proposal-20261009-01/proposal-final.json"

class ActiveGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw=SPEC.read_bytes()
        cls.spec=json.loads(raw)
        cls.design=build_design(cls.spec,spec_sha=hashlib.sha256(raw).hexdigest())
    def test_module_inline_adapter_preserves_frozen_physics_and_probe(self):
        model,contract=lower(self.design)
        archived=ROOT/'week12/results/s3-c-active-eightwheel-execution-20261009-01/eightwheel-signed-work-execution-20261010/generated/task_eea4728ef1c44680a5310262b562dee3'
        original=(archived/'V11GeneratedActive.mo').read_text(encoding='utf8')
        wrapper='  function referenceGeometryInline\n    extends V11ActiveClusterC3.referenceGeometry;\n    annotation(Inline=true);\n  end referenceGeometryInline;'
        self.assertEqual(model.count(wrapper),1)
        self.assertEqual(model.replace(wrapper+'\n\n','').replace('ref=referenceGeometryInline(', 'ref=C.referenceGeometry('), original)
        self.assertEqual(json.loads(json.dumps(contract)),json.loads((archived/'output-contract.json').read_bytes()))
        get_probe=lambda text: re.search(r'  model Probe\b[\s\S]*?  end Probe;',text).group(0)
        self.assertEqual(get_probe(model),get_probe(original))
        self.assertNotIn('referenceGeometry',get_probe(model))

    def test_binding_rejects_unapproved_bytes(self):
        with self.assertRaisesRegex(UnsupportedDesign,"UNBOUND_SPEC"):
            build_design(self.spec,spec_sha="0"*64)
    def test_primitive_payload_count_and_moving_support_tree(self):
        graph=self.design["graph"]
        self.assertEqual(len(graph["bodies"]),7)
        self.assertEqual(len(graph["joints"]),6)
        self.assertAlmostEqual(sum(b["m"] for b in graph["bodies"]),85,places=8)
        primitive=[p for b in graph["bodies"] for p in b["primitives"]]
        self.assertEqual(sum(p["m"] for p in primitive if p["source_id"]=="payload"),20)
        self.assertEqual(len({p["source_id"] for p in primitive}),len(primitive))
        ends={(c.get("from_port"),c.get("to_port")) for c in graph["connections"]}
        for joint in graph["joints"]:
            self.assertIn((joint["drive_id"]+".shaft",joint["id"]+".axis"),ends)
            self.assertIn((joint["drive_id"]+".support",joint["id"]+".support"),ends)
    def test_full_route_continuity_and_identity_preserving_reflection(self):
        for n in (3,20):
            route=flight_plan(self.design,n)
            for first,second in zip(route,route[1:]):
                a=_geometry(first,first["u1"],self.design,n)
                b=_geometry(second,second["u0"],self.design,n)
                for wid in a:
                    for x,y in zip(a[wid],b[wid]):self.assertAlmostEqual(x,y,places=9)
            end=route[-1]
            up=_geometry(end,end["u1"],self.design,n)
            down=_geometry(end,end["u1"],self.design,n,True)
            edge=(n-1)*self.design["geometry"]["D"]
            for wid in up:
                mapped=("R" if wid[0]=="F" else "F")+wid[1:]
                self.assertAlmostEqual(down[mapped][0],edge-up[wid][0],places=9)
                self.assertAlmostEqual(down[mapped][1],up[wid][1],places=9)
    def test_actual_model_output_contract_is_per_feature_and_scoped(self):
        model,contract=lower(self.design)
        self.assertNotIn("reinit(",model)
        self.assertNotIn("Sources.Position",model)
        self.assertNotIn("Sources.Speed",model)
        self.assertIn("ACTUAL_COMPLETE_CRUISING",model)
        for name,scene in contract["scenes"].items():
            self.assertTrue(scene["columns"])
            self.assertTrue(scene["features"])
            for feature in scene["features"]:
                self.assertTrue({"Fn","Ft","active","penetration","slip"}<=set(feature["columns"]))
            if name not in ("active_joint_energy","one_twin_module_load_cycle"):
                self.assertEqual(sum(d["multiplicity"] for d in scene["drives"]),12)
        self.assertEqual(contract["scenes"]["one_twin_module_load_cycle"]["scope"],"one_twin_module_pitch_constrained_half_vehicle_load_fixture")
    def test_sysml_frame_rename_preserves_graph_connections(self):
        source=_sysml(self.design)
        self.assertNotIn('port frame :',source)
        self.assertNotIn('.frame to',source)
        for j in self.design['graph']['joints']:
            self.assertIn('connect '+j['parent']+'.mechanicalFrame to '+j['id']+'.parentFrame;',source)
            self.assertIn('connect '+j['id']+'.childFrame to '+j['child']+'.mechanicalFrame;',source)
            self.assertIn('connect '+j['drive_id']+'.shaft to '+j['id']+'.axis;',source)
            self.assertIn('connect '+j['drive_id']+'.support to '+j['id']+'.support;',source)
    def test_signed_operands_are_legal_in_all_modelica_scene_expressions(self):
        model,_=lower(self.design)
        # Compiler actually rejected rollingLoss term+(-term) without brackets;
        # check all generated arithmetic, including negative geometry offsets.
        bad=re.findall(r'[+*/-]\s*-(?=[A-Za-z0-9_(])',model)
        self.assertEqual(bad,[])
        self.assertIn('rollingLoss=(-rr_F_A*F_A.omega)+(-rr_F_B*F_B.omega)',model)
        self.assertIn('refWheelX_F_B=ref[1]+(-',model)
    def test_real_generated_package_file_has_registered_root_name(self):
        target=ROOT/'week12/results/s3-c-active-eightwheel-execution-20261009-01'
        with tempfile.TemporaryDirectory(prefix='package-name-unit-',dir=target) as temporary:
            manifest=generate(SPEC,Path(temporary)/'new-artifacts')
            package=Path(manifest['package_path'])
            roots={value.split('.')[0] for value in manifest['model_class_by_scene'].values()}
            self.assertEqual(roots,{'V11GeneratedActive'})
            self.assertEqual(package.name,'V11GeneratedActive.mo')
            self.assertIn('package V11GeneratedActive',package.read_text(encoding='utf-8'))

if __name__=="__main__":unittest.main()
