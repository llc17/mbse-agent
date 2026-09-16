from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from week11.scripts.audit_version import audit, GENERATION_FILES, VERSION_FILES
from week11.src.store import WEEK11_ROOT

ANCHOR = WEEK11_ROOT / 'results/wp2-rc-integration-final-20260914'


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WEEK11_ROOT, prefix='audit-test-')
        self.root = Path(self.temp.name)
        self.version = self.root / 'projects/wp2-rc-public/versions/v000001'
        self.version.mkdir(parents=True)
        for name in GENERATION_FILES | {'manifest.json'}:
            shutil.copyfile(ANCHOR / name, self.version / name)
        binding = json.loads((self.version / 'generation-binding.json').read_text(encoding='utf-8'))
        self.write('confirmation.json', {'schema_version':'v10-confirmation-1', 'project_id':'wp2-rc-public', 'proposal_id':'fixture-proposal', 'spec_digest':binding['spec_digest'], 'displayed_digest':'a'*64, 'confirmed_at':'fixture-not-human', 'operation':'create', 'baseline_version_id':None})
        self.manifests()
        self.patcher = patch('week11.scripts.audit_version.WEEK11_ROOT', self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def write(self, name, data):
        (self.version / name).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def manifests(self):
        for name, schema, files in [('manifest.json','v10-generation-manifest-1',GENERATION_FILES), ('version-manifest.json','v10-version-manifest-1',VERSION_FILES)]:
            self.write(name, {'schema_version':schema, 'files':{item:{'bytes':len((self.version/item).read_bytes()),'sha256':hashlib.sha256((self.version/item).read_bytes()).hexdigest()} for item in files}})

    def test_real_saved_run_fixture_and_byte_corruption(self):
        result = audit(self.version)
        self.assertTrue(all(result[k] for k in ('technical','binding','evidence')), result)
        (self.version / 'model.mo').write_text('changed', encoding='utf-8')
        self.assertFalse(audit(self.version)['technical'])

    def test_coherent_hash_update_does_not_hide_missing_check(self):
        run = json.loads((self.version/'run-result.json').read_text(encoding='utf-8'))
        run['checks'] = [x for x in run['checks'] if x['check_type'] != 'modelica_compile']
        self.write('run-result.json', run)
        self.manifests()
        self.assertFalse(audit(self.version)['technical'])

    def test_confirmation_other_project_rejected_even_with_updated_hashes(self):
        confirmation = json.loads((self.version/'confirmation.json').read_text(encoding='utf-8'))
        confirmation['project_id'] = 'another-project'
        self.write('confirmation.json', confirmation)
        self.manifests()
        result = audit(self.version)
        self.assertTrue(result['technical'], result)
        self.assertFalse(result['binding'])

    def test_missing_manifest_entry_and_wrong_external_digest(self):
        self.assertFalse(audit(self.version, '0'*64)['technical'])
        manifest = json.loads((self.version/'version-manifest.json').read_text(encoding='utf-8'))
        del manifest['files']['confirmation.json']
        self.write('version-manifest.json', manifest)
        self.assertFalse(audit(self.version)['technical'])


if __name__ == '__main__':
    unittest.main()
