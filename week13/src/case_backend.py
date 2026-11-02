"""Ordinary ArtifactService/Store with explicit external-reference baseline."""
from importlib import import_module
from .case_paths import ROOT,RUN,authorization,require_time,read_json,sha_file

def module(name):return import_module('week13.src.inherited.'+name)

def verify_inherited_sources():
    from .case_paths import REPO
    for item in read_json(ROOT/'configs/case_sources.json')['files']:
        if sha_file(REPO/item['source'])!=item['source_sha256']:raise ValueError('inherited source drift: '+item['source'])

def service(db_path,session_id,*,worker_manager=None,read_only=False,task_context=None,project_root=None):
    verify_inherited_sources()
    from .case_scope import check_delta
    class CaseService(module('artifact_service').ArtifactService):
        def proposal_prepare(self,spec):
            from .inherited.artifact_preparation import proposal_preparation
            with proposal_preparation(self):
                return self._case_proposal_prepare(spec)
        def _case_proposal_prepare(self,spec):
            from .case_diagnostics import phase
            if task_context:
                from .case_requests import require_execution
                require_execution(task_context['request_id'])
                with phase('NORMALIZE'):normalized,_=self.normalize_candidate(spec)
                with phase('DELTA'):check_delta(self,normalized,task_context=task_context)
                return super()._proposal_prepare(normalized)
            require_time()
            with phase('NORMALIZE'):normalized,_=self.normalize_candidate(spec)
            with phase('DELTA'):check_delta(self,normalized)
            from .case_session import check_execution_policy
            with phase('POLICY'):check_execution_policy(normalized)
            result=super()._proposal_prepare(normalized)
            return result
        def execution_start(self,proposal_id,request_id):
            if task_context:
                from .case_requests import require_execution
                require_execution(task_context['request_id'])
                check_delta(self,self.store.proposal(proposal_id)['spec'],task_context=task_context)
                return super().execution_start(proposal_id,request_id)
            require_time();check_delta(self,self.store.proposal(proposal_id)['spec'])
            from .case_session import check_execution_policy
            check_execution_policy(self.store.proposal(proposal_id)['spec'])
            return super().execution_start(proposal_id,request_id)
        def _artifact_version(self,version_id):
            if version_id=='v000000':
                from pathlib import Path
                from .case_paths import PROJECT_ROOT
                record=read_json(self.project_root/'imported-reference.json')
                if self._current_project()['project_id']!=record['project_id']:raise ValueError('wrong reference project')
                from .inherited.artifact_baseline import resolve,reference
                return resolve(self,reference(record))
            record=super()._artifact_version(version_id)
            from .case_contact_review import with_review
            return with_review(record,self.project_root)
        def baseline_context(self):
            from .inherited.artifact_baseline import selected_record,reference
            record=selected_record(self)
            return dict(reference=reference(record),spec=record['frozen_spec'],record_kind=record['record_kind'],evidence_status=record['evidence_status'],scope='USER_SUCCESS_REFERENCE_NOT_CURRENT_EXECUTION',published=False) if record else None
    context=module('model_artifact').ExecutionContext.load(task_context['execution_context_path'] if task_context else RUN/'context.json') if not read_only else None
    result=CaseService(db_path,session_id,context=context,worker_manager=worker_manager or object(),read_only=read_only)
    from pathlib import Path
    from .case_paths import PROJECT_ROOT
    result.project_root=Path(task_context['project_root']) if task_context else Path(project_root) if project_root is not None else PROJECT_ROOT
    return result
