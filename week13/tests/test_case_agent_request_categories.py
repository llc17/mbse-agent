"""Stage/category regression, isolated and no product or execution calls."""
import asyncio,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock
from week13.src.case_paths import ROOT
from week13.src.case_runtime import CaseRuntime
from week13.src.case_taskflow import TaskFlow

class Categories(unittest.TestCase):
    def test_run_explanation_fulfills_reserved_category_and_ask_stays_consultation(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'results') as temp:
            flow=TaskFlow.__new__(TaskFlow);flow.root=Path(temp);flow.ctx=dict(raw_request='isolated fixture',baseline_snapshot={})
            state=dict(latest_request_execution=None)
            flow.state=lambda:state
            flow.call=AsyncMock(return_value=dict(content='isolated fixture answer',tool_calls=[],assistant_message=dict(role='assistant',content='fixture',reasoning_content='fixture protocol')))
            asyncio.run(flow.ask('fixture',explain=True));stage=flow.call.await_args.args[0]
            self.assertEqual(stage,'explain_0');self.assertEqual(CaseRuntime._category(None,'case_'+stage+'_fixture'),'explanation')
            asyncio.run(flow.ask('fixture'));stage=flow.call.await_args.args[0]
            self.assertEqual(stage,'ask_1');self.assertEqual(CaseRuntime._category(None,'case_'+stage+'_fixture'),'consultation')
            asyncio.run(flow.ask('fixture',reopen=True));stage=flow.call.await_args.args[0]
            self.assertEqual(stage,'reopen_2');self.assertEqual(CaseRuntime._category(None,'case_'+stage+'_fixture'),'reopen')

if __name__=='__main__':unittest.main()
