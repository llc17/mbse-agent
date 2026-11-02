"""Driver-error recording fixture only; no business stage or runtime launch."""
import importlib.util
from pathlib import Path
import tempfile,unittest
from week13.src.case_paths import ROOT,read_json


class ErrorJournal(unittest.TestCase):
    def test_repeat_stage_failure_appends_and_preserves_first_receipt(self):
        package=ROOT/'results/strict-output-contact-followup-20261022-01'
        spec=importlib.util.spec_from_file_location('fixture_followup_driver',package/'execute_followup.py')
        driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)
        with tempfile.TemporaryDirectory(prefix='error-journal-local-',dir=package) as folder:
            target=Path(folder).resolve();self.assertTrue(target.is_relative_to(package.resolve()))
            driver.PACKAGE=target
            first=driver.save_stage_error('fixture','plan',{'error':'first'})
            before=first.read_bytes()
            second=driver.save_stage_error('fixture','plan',{'error':'second'})
            self.assertNotEqual(first,second);self.assertEqual(first.read_bytes(),before)
            self.assertEqual(read_json(second),{'error':'second'})


if __name__=='__main__':unittest.main()
