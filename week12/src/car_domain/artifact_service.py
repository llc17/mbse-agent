"""Thin source-artifact hooks on the V10 project/proposal/confirmation lifecycle."""
from copy import deepcopy
import json,uuid
from pathlib import Path
from week11.src.service import Service,_public_proposal
from week11.src.store import canonical_json,digest_json,safe_id,utc_now
from .store_adapter import CarStore,ReadOnlyCarStore,TERMINAL
from .contracts import WEEK12_ROOT,inside_week12
from .model_artifact import ExecutionContext,validate_artifact,sha

class ArtifactStore(CarStore):
    def _initialize(self):
        super()._initialize()
        with self.transaction() as db:db.execute('CREATE TABLE IF NOT EXISTS artifact_baseline_selections(session_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,binding_json TEXT NOT NULL)')

    def select_artifact_baseline(self,session_id,project_id,binding):
        with self.transaction() as db:
            row=db.execute('SELECT current_project_id FROM sessions WHERE session_id=?',(session_id,)).fetchone()
            if row is None or row[0]!=project_id:raise ValueError('baseline project not selected')
            db.execute('DELETE FROM artifact_baseline_selections WHERE session_id=?',(session_id,))
            if binding is not None:db.execute('INSERT INTO artifact_baseline_selections VALUES(?,?,?)',(session_id,project_id,canonical_json(binding)))
            db.execute('UPDATE projects SET revision=revision+1 WHERE project_id=?',(project_id,))
            db.execute("UPDATE proposals SET state='invalidated',updated_at=? WHERE project_id=? AND state IN ('awaiting_confirmation','approved')",(utc_now(),project_id))

    def stage_artifact(self,task_id,owner,result):
        with self.transaction() as db:
            self._owned(db,task_id,owner,{'running'})
            db.execute("UPDATE tasks SET result_json=?,progress='artifact evidence ready; awaiting worker exit',updated_at=? WHERE task_id=? AND worker_owner=?",(canonical_json(result),utc_now(),task_id,owner))

    def finish_task(self,task_id,state,*,owner=None,result=None,error=''):
        if state not in TERMINAL:raise ValueError('invalid terminal state')
        with self.transaction() as db:
            stored=db.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone()
            if stored is None:raise ValueError('unknown task')
            owner=owner or stored['worker_owner']
            row=self._owned(db,task_id,owner,{'queued','running','cancel_requested'}|TERMINAL)
            if row['state'] in TERMINAL:
                if row['state']!=state or row['result_json']!=(canonical_json(result) if result is not None else None):raise ValueError('terminal evidence immutable')
            else:
                if row['state']=='cancel_requested' and state=='succeeded':raise ValueError('accepted cancellation cannot become success')
                if state=='succeeded' and (not isinstance(result,dict) or result.get('chain_status')!='PASS' or result.get('worker_exit_code')!=0):raise ValueError('artifact chain success requires actual exit0')
                db.execute('UPDATE tasks SET state=?,progress=?,result_json=?,error=?,updated_at=? WHERE task_id=? AND worker_owner=?',(state,state,canonical_json(result) if result is not None else None,error[:2000],utc_now(),task_id,owner))
                db.execute("UPDATE proposals SET state='executed',updated_at=? WHERE proposal_id=? AND state='approved'",(utc_now(),row['proposal_id']))
        return self.task(task_id)

class ArtifactService(Service):
    def __init__(self,db_path,session_id,*,context=None,worker_manager=None,recover=False,read_only=False):
        self.read_only=read_only
        if read_only:
            safe_id(session_id,'session_id');self.session_id=session_id
            self.store=ReadOnlyCarStore(inside_week12(db_path));self.store.ensure_session(session_id)
            self.context=None;self.worker_manager=None
            return
        self.context=context if isinstance(context,ExecutionContext) else ExecutionContext.load(context)
        self.context.assert_current();safe_id(session_id,'session_id')
        self.session_id=session_id;self.store=ArtifactStore(inside_week12(db_path));self.store.ensure_session(session_id)
        if recover:self.store.recover_orphans()
        if worker_manager is None:
            from .artifact_worker import ArtifactWorkerManager
            worker_manager=ArtifactWorkerManager(self.store,self.context)
        self.worker_manager=worker_manager

    def normalize_candidate(self,spec):
        """Resolve source IDs from the current trusted Store, then baseline metadata.

        Explicit text remains immutable: an altered string is rejected, never
        repaired. This shared path is used by both CLI preflight and MCP prepare.
        """
        if not isinstance(spec,dict):return spec,{}
        candidate=deepcopy(spec);declared=candidate.get('sources')
        if isinstance(declared,list):
            project=self._current_project()
            with self.store.reading() as db:
                rows=db.execute('SELECT source_id,text FROM sources WHERE session_id=? AND project_id=?',(self.session_id,project['project_id'])).fetchall()
            trusted={r['source_id']:r['text'] for r in rows};resolved=[]
            for source in declared:
                if not isinstance(source,dict) or set(source) not in ({'id'},{'id','text'}) or not isinstance(source.get('id'),str) or source['id'] not in trusted:raise ValueError('unknown/invalid trusted source reference')
                text=trusted[source['id']]
                if 'text' in source and source['text']!=text:raise ValueError('spec source text differs from trusted CLI input')
                resolved.append(dict(id=source['id'],text=text))
            candidate['sources']=resolved
        from .artifact_baseline import normalize_candidate
        candidate,inherited=normalize_candidate(self,candidate)
        from .artifact_sysml import normalize_package_wrapper
        artifact=candidate.get('artifact')
        if isinstance(artifact,dict) and 'sysml_source' in artifact:artifact['sysml_source'],_=normalize_package_wrapper(artifact['sysml_source'])
        from .artifact_source_correction import normalize_bound_source_notes
        candidate,_=normalize_bound_source_notes(candidate,Path(self.store.db_path).parent)
        return candidate,inherited

    def proposal_prepare(self,spec):
        if self.read_only:raise ValueError('historical artifact service is read-only')
        from .artifact_sysml import normalize_package_wrapper
        artifact=spec.get('artifact') if isinstance(spec,dict) else None
        _,normalization=normalize_package_wrapper(artifact.get('sysml_source')) if isinstance(artifact,dict) else (None,None)
        from .artifact_source_correction import normalize_bound_source_notes
        _,notes=normalize_bound_source_notes(spec,Path(self.store.db_path).parent)
        spec,inherited=self.normalize_candidate(spec)
        checked=validate_artifact(spec,require_sysml=True)
        if not checked['ready']:return dict(ok=False,reason='spec_not_ready',issues=checked['issues'])
        project=self._current_project();self._verify_sources(project['project_id'],checked['spec'])
        if project['selected_version_id']!=project['current_version_id']:raise ValueError('historical selected version is read-only')
        if self.context.mode.startswith('TEST_ONLY') and not project['name'].startswith('TEST_ONLY'):raise ValueError('test context requires TEST_ONLY project')
        self.context.assert_current()
        display=dict(schema_version='v11-artifact-display-1',project_id=project['project_id'],project_name=project['name'],operation='create',baseline_version_id=project['current_version_id'],project_revision=project['revision'],spec_digest=checked['spec_digest'],source_sha256=checked['source_sha256'],requirements=deepcopy(spec['requirements']),sources=deepcopy(spec['sources']),model_design=deepcopy(spec['design']),artifact=deepcopy(spec['artifact']),libraries=deepcopy(spec['libraries']),settings=deepcopy(spec['settings']),outputs=deepcopy(spec['outputs']),checks=deepcopy(spec['checks']),execution_context_id=self.context.context_id,execution_context_sha256=self.context.digest,execution_mode=self.context.mode,physical_acceptance_status='NOT_VERIFIED')
        if 'trajectory_contract' in spec:display['trajectory_contract']=deepcopy(spec['trajectory_contract'])
        if normalization:display['source_normalizations']=[normalization]
        if notes:
            if notes['normalized_candidate_sha256']!=digest_json(checked['spec']):raise ValueError('note normalization does not bind this proposal')
            display.update(unapproved_model_notes=deepcopy(notes['unapproved_model_notes']),generation_protocol_normalizations=[notes])
        from .artifact_baseline import selected_record,reference,changes
        baseline=selected_record(self)
        if baseline is None and 'baseline_ref' in spec:raise ValueError('baseline must be explicitly selected locally')
        if baseline is not None and spec.get('baseline_ref')!=reference(baseline):raise ValueError('candidate refers to a different artifact baseline')
        operation='modify' if baseline else 'create';display['operation']=operation
        if baseline:display.update(artifact_baseline=reference(baseline),changes=changes(baseline['frozen_spec'],checked['spec']),artifact_baseline_published=False)
        if inherited:display['inherited_fields']=inherited
        proposal=self.store.insert_proposal(dict(proposal_id='prop_'+uuid.uuid4().hex,session_id=self.session_id,project_id=project['project_id'],operation=operation,baseline_version_id=project['current_version_id'],project_revision=project['revision'],spec=checked['spec'],spec_digest=checked['spec_digest'],display=display,displayed_digest=digest_json(display)))
        return self._ok('proposal',_public_proposal(proposal))

    def execution_start(self,proposal_id,request_id):
        if self.read_only:raise ValueError('historical artifact service is read-only')
        self.context.assert_current()
        if not self.context.allow_execution:raise ValueError('caller context permits proposal only, no Modelica execution')
        proposal=self.store.proposal(proposal_id);self._require_current_project(proposal['project_id'])
        checked=validate_artifact(proposal['spec'],require_sysml=True)
        if not checked['ready'] or checked['spec_digest']!=proposal['spec_digest'] or digest_json(proposal['display'])!=proposal['displayed_digest']:raise ValueError('frozen artifact/source or display changed')
        if proposal['display'].get('execution_context_sha256')!=self.context.digest:raise ValueError('trusted execution context mismatch')
        self._verify_sources(proposal['project_id'],proposal['spec'])
        from .artifact_baseline import verify_proposal
        verify_proposal(self,proposal,require_selection=True,require_confirmation=True)
        with self.store.reading() as db:
            existing=db.execute('SELECT task_id FROM tasks WHERE session_id=? AND request_id=?',(self.session_id,request_id)).fetchone()
            displays=db.execute('SELECT p.display_json FROM tasks t JOIN proposals p ON p.proposal_id=t.proposal_id').fetchall()
            count=sum(json.loads(row[0]).get('execution_context_id')==self.context.context_id for row in displays)
        if existing is None and count>=self.context.max_tasks:raise ValueError('finite artifact task cap exhausted')
        return super().execution_start(proposal_id,request_id)

    def confirm_local(self,proposal_id,displayed_digest):
        if self.read_only:raise ValueError('historical artifact service is read-only')
        from .artifact_baseline import verify_proposal
        proposal=self.store.proposal(proposal_id);verify_proposal(self,proposal,require_selection=True)
        checked=validate_artifact(proposal['spec'],require_sysml=True)
        if not checked['ready']:raise ValueError('new confirmation requires SysML: '+str(checked['issues']))
        confirmed=super().confirm_local(proposal_id,displayed_digest)
        if self.context.mode.startswith('TEST_ONLY') or proposal['operation']=='modify':
            with self.store.transaction() as db:
                row=db.execute('SELECT confirmation_json FROM proposals WHERE proposal_id=?',(proposal_id,)).fetchone()
                confirmation=json.loads(row['confirmation_json'])
                if self.context.mode.startswith('TEST_ONLY'):confirmation.update(channel='TEST_ONLY_CLI_CONFIRMATION',human_new_approval=False,test_only=True)
                if proposal['operation']=='modify':confirmation['artifact_baseline']=deepcopy(proposal['display']['artifact_baseline'])
                db.execute('UPDATE proposals SET confirmation_json=? WHERE proposal_id=?',(canonical_json(confirmation),proposal_id))
            return self._ok('proposal',_public_proposal(self.store.proposal(proposal_id)))
        return confirmed

    def confirm_design_delegated_local(self,proposal_id,record,approval,*,current_budget):
        """Local-only delegation bound to a displayed finite design, not source review.

        Intentionally absent from MCP/model tools. Reuses frozen proposal and
        baseline checks, but records the actual design-delegation channel.
        """
        from .artifact_continuous import digest,compare_candidate,capacity_budget_matches
        if self.read_only or not self.context.allow_execution:raise ValueError('design delegation has no execution permission')
        self.context.assert_current()
        binding={k:v for k,v in record.items() if k not in {'design_id','displayed_digest'}}
        if record['displayed_digest']!=digest(binding) or record['design_id']!='flow_'+digest(binding):raise ValueError('design receipt drift')
        project=self._current_project()
        with self.store.reading() as db:rows=db.execute('SELECT source_id,text FROM sources WHERE session_id=? AND project_id=? ORDER BY rowid',(self.session_id,project['project_id'])).fetchall()
        sources=[dict(id=r['source_id'],text=r['text']) for r in rows]
        if record['session_id']!=self.session_id or record['project_id']!=project['project_id'] or record['sources']!=sources or not capacity_budget_matches(Path(self.store.db_path).parent,record,current_budget) or current_budget['context_sha256']!=self.context.digest:raise ValueError('design source/budget/context drift')
        if approval.get('schema')!='v11-continuous-design-approval-1' or approval.get('channel')!='trusted_local_design_delegation' or approval.get('approval_text') not in {'确认','批准','同意','CONFIRM'} or approval.get('source_reviewed_by_human') is not False or any(approval.get(k)!=record[k] for k in ('design_id','displayed_digest','session_id','project_id','budget')) or approval.get('source_snapshot')!=sources:raise ValueError('specific local design delegation required')
        root=Path(self.store.db_path).parent/'cli-continuations'
        if json.loads((root/(record['design_id']+'-approval.json')).read_text(encoding='utf8'))!=approval or (root/(record['design_id']+'-declined.json')).exists():raise ValueError('local design delegation missing or declined')
        proposal=self.store.proposal(proposal_id)
        if proposal['display'].get('execution_context_sha256')!=self.context.digest:raise ValueError('proposal context changed')
        checked=validate_artifact(proposal['spec'],require_sysml=True)
        if not checked['ready'] or checked['spec_digest']!=proposal['spec_digest']:raise ValueError('generated artifact changed')
        evidence=compare_candidate(record['commitment'],checked['spec'])
        self._verify_sources(project['project_id'],checked['spec'])
        # Store.confirm enforces current session, revision, frozen spec/display
        # and immutable state. The resulting receipt explicitly names delegation.
        self.confirm_local(proposal_id,proposal['displayed_digest'])
        with self.store.transaction() as db:
            row=db.execute('SELECT confirmation_json FROM proposals WHERE proposal_id=?',(proposal_id,)).fetchone()
            confirmation=json.loads(row['confirmation_json'])
            confirmation.update(channel='TEST_ONLY_DESIGN_DELEGATION' if self.context.mode.startswith('TEST_ONLY') else 'trusted_local_design_delegation',source_reviewed_by_human=False,design_id=record['design_id'],design_digest=record['displayed_digest'],design_approval=approval,commitment_check=evidence)
            if proposal['display'].get('source_normalizations'):confirmation['source_normalizations']=deepcopy(proposal['display']['source_normalizations'])
            if proposal['display'].get('unapproved_model_notes'):confirmation.update(unapproved_model_notes=deepcopy(proposal['display']['unapproved_model_notes']),generation_protocol_normalizations=deepcopy(proposal['display']['generation_protocol_normalizations']))
            db.execute('UPDATE proposals SET confirmation_json=? WHERE proposal_id=?',(canonical_json(confirmation),proposal_id))
        return self._ok('proposal',_public_proposal(self.store.proposal(proposal_id)))

    def versions_select(self,version_id):
        if self.read_only:raise ValueError('historical artifact service is read-only')
        from .artifact_baseline import reference,resolve
        project=self._current_project()
        ref=None if version_id=='none' else reference(self._artifact_version(version_id))
        if ref:resolve(self,ref)
        self.store.select_artifact_baseline(self.session_id,project['project_id'],ref)
        return self._ok('project',self._current_project(),artifact_baseline=ref,baseline_published=False)

    def baseline_context(self):
        from .artifact_baseline import selected_record,reference
        record=selected_record(self)
        return dict(reference=reference(record),spec=deepcopy(record['frozen_spec']),published=False,scope='VERIFIED_EXECUTION_BASELINE_NOT_CERTIFIED_VEHICLE') if record else None

    def evidence_query(self,question,version_id=None):
        question,separator,assessment_path=question.partition('|trajectory:')
        if not question.startswith('task:'):raise ValueError('artifact query syntax task:<id>')
        task=self.store.task(safe_id(question[5:],'task_id'));self._require_current_project(task['project_id'])
        if version_id is not None and version_id!=task['version_id']:raise ValueError('task/version mismatch')
        from .artifact_history import snapshot
        record,manifest=snapshot(self,task)
        evidence=dict(task_id=task['task_id'],outcome=task.get('result'),snapshot=record,manifest=manifest,evidence_status=record['evidence_status'],read_only=True)
        if separator:
            from .artifact_trajectory import supplemental_assessment
            path=inside_week12(assessment_path)
            if not path.is_relative_to(WEEK12_ROOT/'results'):raise ValueError('supplemental assessment document must be in week12/results')
            evidence['supplemental_trajectory']=supplemental_assessment(record,manifest,json.loads(path.read_bytes()))
        return self._ok('evidence',evidence)

    def versions_list(self):
        from .artifact_history import snapshot
        project=self._current_project()
        items=[snapshot(self,t)[0] for t in self.store.tasks_for_project(project['project_id']) if self.store.proposal(t['proposal_id'])['spec'].get('backend')=='model-artifact']
        return self._ok('versions',dict(current_version_id=project['current_version_id'],selected_version_id=project['selected_version_id'],record_kind='ARTIFACT_EXECUTION_SNAPSHOTS',published=False,read_only=True,items=items))

    def _artifact_version(self,version_id):
        safe_id(version_id,'version_id');project=self._current_project()
        matches=[t for t in self.store.tasks_for_project(project['project_id']) if t['version_id']==version_id and self.store.proposal(t['proposal_id'])['spec'].get('backend')=='model-artifact']
        if len(matches)!=1:raise ValueError('snapshot not uniquely present in current project')
        from .artifact_history import snapshot
        return snapshot(self,matches[0])[0]

    def versions_compare(self,before_version_id,after_version_id):
        from .artifact_history import comparison
        return self._ok('comparison',comparison(self._artifact_version(before_version_id),self._artifact_version(after_version_id)))

    def project_status(self):
        project=self._current_project();items=self.versions_list()['versions']['items']
        from .artifact_baseline import selection
        return self._ok('status',dict(project=project,latest_task=items[-1] if items else None,snapshot_count=len(items),published_version_count=len(self.store.versions(project['project_id'])),read_only=self.read_only,artifact_baseline=selection(self)))

    def execution_status(self,task_id):
        from .artifact_history import snapshot
        task=self.store.task(safe_id(task_id,'task_id'));record,_=snapshot(self,task)
        return self._ok('task',task,snapshot=record,read_only=self.read_only)
