"""Compiled-constant parser fixtures; no OMC/API/process or historical writes."""
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from week13.src.case_contact_boundary import compiled_constants,check_csv
from week13.tests.test_case_contact_numerical_checks_ui import fixture_rows

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/contact-numerics-20261022-01'
TOOL=OUT/'boundary-01/tool'


class CompiledBoundaryReadbackChecks(unittest.TestCase):
    def setUp(self):
        self.source=(TOOL/'model.mo').read_text(encoding='utf8')
        self.temp=tempfile.TemporaryDirectory(prefix='constant-parser-fixture-',dir=OUT)
        self.folder=Path(self.temp.name);self.xml=self.folder/'model_init.xml';self.init=self.folder/'model_08bnd.c'
        tree=ET.parse(TOOL/'model_init.xml')
        for node in tree.getroot().iter('ScalarVariable'):
            if node.get('fileName')==str((TOOL/'model.mo').resolve()).replace('\\','/'):
                node.set('fileName',str(self.folder/'model.mo').replace('\\','/'))
        tree.write(self.xml,encoding='utf8')
        self.init.write_text((TOOL/'model_08bnd.c').read_text(encoding='utf8'),encoding='utf8')

    def tearDown(self):self.temp.cleanup()

    def test_XML_plus_actual_initializer_mapping_retains_no_runtime_claim(self):
        result=compiled_constants(self.source,self.xml,self.init)
        self.assertEqual(result['values'],{'mu_s':.85,'R_w[1]':.07,'R_w[2]':.127,'R_w[3]':.127,'T_w[1]':25.,'T_w[2]':60.,'T_w[3]':60.})
        self.assertTrue(all(row['xml_start_present'] is False for row in result['alias_bindings'].values()))
        self.assertIn('NOT_RUNTIME_OUTPUT',result['observation_role'])

    def test_changed_direct_XML_constant_is_rejected(self):
        tree=ET.parse(self.xml)
        for node in tree.getroot().iter('ScalarVariable'):
            if node.get('name')=='mu_s':node.find('Real').set('start','0.8')
        tree.write(self.xml,encoding='utf8')
        with self.assertRaisesRegex(ValueError,'XML/source parameter conflict'):
            compiled_constants(self.source,self.xml,self.init)

    def test_changed_compiled_initializer_alias_is_rejected(self):
        self.init.write_text(self.init.read_text(encoding='utf8').replace('realParameter[5] /* R_nose PARAM */','realParameter[4] /* R_main PARAM */',1),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'initializer/source alias mapping differs'):
            compiled_constants(self.source,self.xml,self.init)

    def test_absent_csv_constant_requires_evidence_and_conflict_is_rejected(self):
        import csv
        rows=list(fixture_rows());path=self.folder/'fixture.csv'
        keys=[name for name in rows[0] if name!='R_w[1]']
        with path.open('w',encoding='utf8',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=keys,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
        with self.assertRaisesRegex(ValueError,'bound compiled constant evidence required'):
            check_csv(path)
        constants=compiled_constants(self.source,self.xml,self.init)['values']
        self.assertEqual(check_csv(path,constants=constants)['status'],'PASS')
        constants=dict(constants,**{'T_w[1]':26.})
        with self.assertRaisesRegex(ValueError,'CSV/compiled constant conflict'):
            check_csv(path,constants=constants)


if __name__=='__main__':unittest.main()
