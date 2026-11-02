import asyncio,json,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
from contextlib import ExitStack
from week13.src import case_cli
from week13.src.case_paths import PACKAGE

class Pipeline(unittest.TestCase):
    def test_repaired_generation_resumes_and_uses_next_refinement_index(self):
        with tempfile.TemporaryDirectory(dir=PACKAGE) as temp,ExitStack() as stack:
            root=Path(temp);stack.enter_context(patch.object(case_cli,'RUN',root))
            (root/'generation-0-failure.json').write_text(json.dumps(dict(mode='initial')),encoding='utf8')
            (root/'generation-1.json').write_text(json.dumps(dict(proposal_id='prop_fixture',spec_digest='fixture',mode='repair',index=1)),encoding='utf8')
            stack.enter_context(patch.object(case_cli,'create',return_value={}))
            stack.enter_context(patch.object(case_cli,'design',new=AsyncMock(return_value=dict(reused=True))))
            generated=AsyncMock(return_value=dict(mode='refine',index=2));stack.enter_context(patch.object(case_cli,'generate',new=generated))
            executed=AsyncMock(return_value=dict(task='synthetic-only'));stack.enter_context(patch.object(case_cli,'execute',new=executed))
            stack.enter_context(patch.object(case_cli,'status',return_value=dict(latest_execution=dict(task_status='PASS',version_id='v000002'))))
            stack.enter_context(patch.object(case_cli,'check_execution_policy',return_value=dict(mode='initial')))
            svc=Mock();svc.store.proposal.return_value=dict(spec_digest='fixture',spec={});stack.enter_context(patch.object(case_cli,'open_service',return_value=svc))
            stack.enter_context(patch.object(case_cli,'compare',return_value=dict(numerical_refinement=dict(status='PASS'))))
            stack.enter_context(patch.object(case_cli,'ask',new=AsyncMock(return_value=dict(answer='fixture'))))
            result=asyncio.run(case_cli.run())
            self.assertEqual(generated.await_args.kwargs,dict(index=2,mode='refine',previous_version='v000002'))
            self.assertEqual(executed.await_args_list[0].kwargs,dict(index=1));self.assertEqual(executed.await_args_list[1].kwargs,dict(index=2))
            self.assertEqual(result['stage_acceptance'],'REQUIRES_PRIMARY_REVIEW_NO_AUTOMATIC_S0_S3_PASS')

if __name__=='__main__':unittest.main()
