"""Offline multi-case list regression; no real catalog, CSV, runtime or checker."""
from contextlib import closing,redirect_stdout
import io,json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_projects as projects,case_projects_cli as cli
from week13.src.case_paths import ROOT

AREA=ROOT/'results/agent-readback-lifecycle-repair-20261101-01/repair1'


class LightCaseListTests(unittest.TestCase):
    def setUp(self):
        AREA.mkdir(parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(prefix='TEST_ONLY-case-list-',dir=AREA)
        self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.cases=[]
        for index,(name,kind) in enumerate((('Legacy','LEGACY_READ_ONLY_CATALOG'),('Zulu','IMPORTED_CASE'),('Alpha','IMPORTED_CASE'))):
            root=self.root/'runs'/f'case_{index}';root.mkdir(parents=True)
            path=root/'projects.sqlite';project=f'project_{index}'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''CREATE TABLE projects(project_id TEXT);
                    CREATE TABLE proposals(proposal_id TEXT,spec_json TEXT);
                    CREATE TABLE tasks(proposal_id TEXT,project_id TEXT,version_id TEXT,state TEXT,result_json TEXT,created_at TEXT);''')
                db.execute('INSERT INTO projects VALUES(?)',(project,));db.commit()
            self.cases.append(dict(case_id='legacy-six-wheel' if index==0 else f'case_{index}',
                name=name,project_id=project,project_root=str(root),db_path=str(path),kind=kind,
                session_id=f'session_{index}',status='EXISTING_PROJECT',support=dict(status='SUPPORTED')))
        self.add_task(0,'v000001','succeeded','PASS');self.add_task(0,'v000002','succeeded','FAIL')
        self.add_task(1,'v000001','succeeded','PASS');self.add_task(1,'v000003','running',None)
        # A different backend and another project must not become latest/default.
        self.add_task(1,'v999999','succeeded','PASS',backend='other')
        self.add_task(1,'v999998','succeeded','PASS',project='foreign_project')

    def add_task(self,index,version,state,task_status,*,backend='model-artifact',project=None):
        case=self.cases[index];proposal='proposal_'+version
        with closing(sqlite3.connect(case['db_path'])) as db:
            db.execute('INSERT INTO proposals VALUES(?,?)',(proposal,json.dumps(dict(backend=backend))))
            result=None if task_status is None else json.dumps(dict(tool_status='PASS',chain_status='PASS',task_status=task_status))
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)',(proposal,project or case['project_id'],version,state,result,version))
            db.commit()

    def context(self):
        from contextlib import ExitStack
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(projects,'ROOT',self.root))
        stack.enter_context(patch.object(projects,'list_cases',return_value=self.cases))
        for name in ('discover_case','_service','verify_case','support_for_source','_read'):
            stack.enter_context(patch.object(projects,name,side_effect=AssertionError('detail path forbidden: '+name)))
        stack.enter_context(patch('week13.src.inherited.artifact_history.snapshot',side_effect=AssertionError('snapshot forbidden')))
        original=Path.open
        def guarded(path,*args,**kwargs):
            if path.suffix.casefold()=='.csv':raise AssertionError('CSV open forbidden')
            return original(path,*args,**kwargs)
        stack.enter_context(patch.object(Path,'open',guarded))
        return stack

    def test_multiple_cases_keep_legacy_id_names_projects_and_stable_catalog_order(self):
        self.context();rows=projects.catalog_summaries()
        self.assertEqual([row['case_id'] for row in rows],[case['case_id'] for case in self.cases])
        self.assertEqual([row['name'] for row in rows],['Legacy','Zulu','Alpha'])
        self.assertEqual([row['project_id'] for row in rows],[case['project_id'] for case in self.cases])
        self.assertEqual(rows[0]['kind'],'LEGACY_READ_ONLY_CATALOG')
        self.assertEqual(rows[0]['latest_version_id'],'v000002')
        self.assertEqual(rows[0]['default_baseline_version_id'],'v000001')
        self.assertEqual(rows[1]['latest_version_id'],'v000003')
        self.assertEqual(rows[2]['latest_version_id'],'v000000')

    def test_recorded_status_stays_separate_from_current_verification(self):
        self.context();rows=projects.catalog_summaries()
        self.assertEqual(rows[0]['latest_recorded_execution']['recorded_task_status'],'FAIL')
        self.assertEqual(rows[1]['latest_recorded_execution']['state'],'running')
        self.assertIsNone(rows[1]['latest_recorded_execution']['recorded_task_status'])
        for row in rows:
            self.assertEqual(row['verification_status'],'NOT_RECHECKED')
            self.assertTrue(row['selection_requires_verification'])
            self.assertNotIn('current_contact_review',row)

    def test_original_cli_list_summary_shape_remains_compatible(self):
        self.context();out=io.StringIO()
        with redirect_stdout(out):code=cli.main(['list'])
        result=json.loads(out.getvalue());self.assertEqual(code,0)
        self.assertEqual(len(result['cases']),3)
        for case,row in zip(self.cases,result['cases']):
            for key in case:self.assertEqual(row[key],case[key])
            self.assertIn('latest_version_id',row);self.assertIn('default_baseline_version_id',row)

    def test_missing_store_is_not_created_or_promoted_and_other_rows_survive(self):
        missing=self.root/'runs/missing/projects.sqlite'
        self.cases[1]=dict(self.cases[1],db_path=str(missing))
        self.context();rows=projects.catalog_summaries()
        self.assertFalse(missing.exists());self.assertEqual(rows[1]['store_metadata_status'],'UNAVAILABLE')
        self.assertIsNone(rows[1]['default_baseline_version_id'])
        self.assertEqual(rows[0]['store_metadata_status'],'AVAILABLE')

    def test_recorded_unsupported_import_does_not_get_automatic_default(self):
        self.cases[2]['support']=dict(status='UNSUPPORTED')
        self.context();rows=projects.catalog_summaries()
        self.assertIsNone(rows[2]['default_baseline_version_id'])
        self.assertEqual(rows[2]['recorded_support_status'],'UNSUPPORTED')


if __name__=='__main__':unittest.main()
