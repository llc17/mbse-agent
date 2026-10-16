"""Bounded scope on the same ledger; closed historical scopes stay immutable."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from .execution import (Route,Stop,DEFAULT_OUT,PROJECT,SPEC,EXPECTED_SPEC,ORDER,
    source_paths,sha,read,save,stamp)
from . import run_policy as policy

PROPOSAL_SHA='4cc44942030d2695cd98b9048a4508e814ba1726c600fd8a5e1707fcaf11c6a6'
KEY='execution_readiness_remediation'

def claim_once(directory,nonce,ordinal):
    claim=Path(directory)/f'remediation-launch-{ordinal:02d}.claim.json'
    try:
        with claim.open('x',encoding='utf-8') as stream:
            json.dump({'nonce':nonce,'pid':os.getpid(),'claimed_utc':stamp()},stream)
    except FileExistsError:
        raise Stop('LAUNCH_ALREADY_CLAIMED_NO_RETRY')
    return claim

def reconcile_start_failure(directory,nonce,error):
    """Safe terminal update also works when Route construction never completed."""
    data=read(Path(directory)/'ledger.json')
    rows=data.get(KEY,{}).get('launchers',[])
    if rows and rows[-1].get('nonce')==nonce and rows[-1]['status'] in ('LAUNCH_REQUESTED','CLAIMED'):
        rows[-1].update(status='FAILED_START',error=str(error),ended_utc=stamp())
        data[KEY].update(status='STOPPED_IMPLEMENTATION',stop_reason=str(error))
        save(Path(directory)/'ledger.json',data)

class RunScope(Route):
    def __init__(self,nonce,mode):
        super().__init__()
        self.proposal=read(self.directory/'execution-readiness-remediation-proposal.json')
        self.auth=read(self.directory/'execution-readiness-remediation-authorization.json')
        self.base=read(self.directory/'ledger-before-execution-remediation.json')
        policy.validate_approval(self.proposal,self.auth,sha(self.directory/'execution-readiness-remediation-proposal.json'),sha(self.directory/'ledger-before-execution-remediation.json'))
        if sha(self.directory/'execution-readiness-remediation-proposal.json')!=PROPOSAL_SHA:
            raise Stop('APPROVED_PROPOSAL_DRIFT')
        self.nonce=nonce;self.mode=mode;self.parent_started=None
        self.guard()
        row=self.data[KEY]['launchers'][-1]
        if row['nonce']!=nonce or row['mode']!=mode or row['status']!='LAUNCH_REQUESTED':
            raise Stop('UNRESERVED_OR_REPEATED_LAUNCH')
        expected=row['source_snapshot']
        for entry in expected:
            if sha(PROJECT/entry['path'])!=entry['sha256']:
                raise Stop('RESERVED_SOURCE_DRIFT:'+entry['path'])
        claim_once(self.directory,nonce,row['ordinal'])
        self.parent_started=time.monotonic()
        row.update(status='CLAIMED',actual_started_utc=stamp(),pid=os.getpid(),
                   scene_leaf_before=self.scene_leaf())
        self.persist()

    def guard(self):
        self.refresh()
        policy.validate_history(self.base,self.data)
        scope=self.data[KEY]
        if scope['authorization_sha256']!=sha(self.directory/'execution-readiness-remediation-authorization.json'):
            raise Stop('SCOPE_AUTHORIZATION_DRIFT')
        if len(scope['launchers'])>3 or len(scope['repairs'])>1 or len(self.data['scene_attempts'])-len(self.base['scene_attempts'])>28:
            raise Stop('FINITE_SCOPE_ATTEMPTS_EXHAUSTED')
        if scope['status'].startswith(('CLOSED','STOPPED')):
            raise Stop('STOPPED_SCOPE_NO_AUTOMATIC_REOPEN')
        for r in self.data['scene_attempts']:
            if any(q.get('status') in ('LAUNCH_REQUESTED','UNCERTAIN') or q.get('uncertain') for q in r.get('processes',[])):
                raise Stop('UNCERTAIN_PRIOR_CHILD')

    def new_scene_rows(self):
        return self.data['scene_attempts'][len(self.base['scene_attempts']):]

    def scene_leaf(self):
        return sum(float(p.get('duration_s',0)) for r in self.new_scene_rows() for p in r.get('processes',[]))

    def process_used(self):
        rows=self.data[KEY]['launchers']
        live=None
        if rows and rows[-1]['status']=='CLAIMED':
            if self.parent_started is None:raise Stop('UNOWNED_LIVE_PARENT')
            live={'nonce':self.nonce,'elapsed_s':time.monotonic()-self.parent_started,
                  'child_s':self.scene_leaf()-rows[-1]['scene_leaf_before']}
        return policy.cumulative_process(self.base['external_process_accounted_s'],
            [p.get('duration_s',0) for r in self.new_scene_rows() for p in r.get('processes',[])],rows,live,
            self.data[KEY].get('frontend_external_s',0))

    def elapsed(self):
        # No wait is deducted solely on a caller-provided label.
        for row in self.data['active_intervals']:
            if row.get('excluded_wait'):
                proof=row.get('independent_wait_evidence')
                if not isinstance(proof,dict):raise Stop('UNBOUND_WAIT_EVIDENCE')
                source=(self.directory/proof.get('file','')).resolve()
                if not source.is_relative_to(self.directory.resolve()) or source.name=='.env':
                    raise Stop('WAIT_EVIDENCE_OUTSIDE_SCOPE')
                if sha(source)!=proof.get('sha256'):raise Stop('WAIT_EVIDENCE_DRIFT')
                actual=read(source)
                if (actual.get('status')!='OBSERVED_WAITING_APPROVAL'
                    or actual.get('started_utc')!=row['started_utc']
                    or actual.get('ended_utc')!=row.get('ended_utc')):
                    raise Stop('WAIT_EVIDENCE_NOT_OBSERVED')
        return policy.active_seconds(self.data['active_intervals'],datetime.now(timezone.utc))

    def limits(self):
        self.guard()
        active=self.elapsed();process=self.process_used()
        newactive=active-self.base['active_time_checkpoint_s']
        newprocess=process-self.base['external_process_accounted_s']
        if (active>=23400 or process>=7000 or active-process>=16400
            or newactive>=9000 or newprocess>=6600 or newactive-newprocess>=2400):
            raise Stop('APPROVED_TOTAL_OR_SCOPE_TIME_LIMIT')
        if self.parent_started is not None:
            row=self.data[KEY]['launchers'][-1]
            if time.monotonic()-self.parent_started-(self.scene_leaf()-row['scene_leaf_before'])>=60:
                raise Stop('PARENT_OVERHEAD_LIMIT')

    def assert_ready(self):
        self.limits()
        if any(r['status']=='RUNNING' for r in self.data['scene_attempts']):
            raise Stop('UNFINISHED_SCENE_RESERVATION')

    def reserve(self,kind,label,wall,round_no=1):
        self.assert_ready()
        if self.mode!='vehicle' or kind!='scene' or round_no!=4+len(self.data[KEY]['repairs']):
            raise Stop('NO_UNAPPROVED_OPERATION_OR_REVISION')
        self.validate_admission()
        policy.next_scene(self.new_scene_rows(),round_no,self.data['gates'],label)
        if len(self.data['scene_attempts'])>=42:raise Stop('ORIGINAL_SCENE_CAP')
        name=label.removesuffix('-refined').removesuffix('-base')
        cap=next(r['wall_each_s'] for r in self.spec['execution_contract_proposal']['scene_caps'] if r['name']==name)
        used=sum(r['reserved_wall_s'] for r in self.new_scene_rows() if r['round']==round_no)
        if wall>cap or used+wall>3180 or self.process_used()+wall>7000 or self.elapsed()+wall>23400:
            raise Stop('SCENE_FULL_RESERVE_EXHAUSTED')
        row={'ordinal':len(self.data['scene_attempts'])+1,'kind':'scene','label':label,
             'round':round_no,'status':'RUNNING','reserved_wall_s':wall,'started_utc':stamp(),'processes':[]}
        self.data['scene_attempts'].append(row);self.persist()
        return 'scene_attempts',len(self.data['scene_attempts'])-1

    def child(self,slot,command,cwd,wall,logs,label):
        self.limits();self.validate_admission()
        row=self.data[slot[0]][slot[1]]
        if len(row['processes'])>=3 or label not in ('build','simulation','verification'):
            raise Stop('CHILD_OPERATION_LIMIT')
        return super().child(slot,command,cwd,min(wall,7000-self.process_used(),
             6600-(self.process_used()-self.base['external_process_accounted_s'])),logs,label)

    def source_bind(self,paths,generated,round_no=1):
        self.assert_ready()
        expected=4+len(self.data[KEY]['repairs'])
        if round_no!=expected or any(r['round']==round_no for r in self.data['source_bindings']):
            raise Stop('SOURCE_REVISION_ALREADY_BOUND_OR_INVALID')
        registered={p.resolve() for p in source_paths()}
        rows=[]
        for p in sorted(set(Path(p).resolve() for p in paths),key=str):
            relative=p.relative_to(PROJECT.resolve()).as_posix()
            if p.name.lower()=='.env' or (p not in registered and not p.is_relative_to(self.directory)):
                raise Stop('SOURCE_NOT_ALLOWLISTED')
            rows.append({'path':relative,'sha256':sha(p)})
        binding={'round':round_no,'proposal_sha256':EXPECTED_SPEC,'files':rows,
                 'generated':generated,'created_utc':stamp(),'scope_authorization_sha256':sha(self.directory/'execution-readiness-remediation-authorization.json')}
        self.data['source_bindings'].append(binding);self.persist();return binding

    def frontend_reusable(self,binding,manifest):
        self.assert_sources(binding)
        result_path=self.directory/'readiness-diagnostic-01/result.json'
        expected=self.proposal['baseline_before_this_preparation']['readiness_result_sha256']
        if sha(result_path)!=expected:raise Stop('ACTUAL_FRONTEND_RECEIPT_DRIFT')
        package=Path(manifest['package_path'])
        if not package.is_absolute():package=Path(binding['generated']['directory'])/package
        if sha(package)!=self.proposal['vehicle']['current_model_sha256']:
            raise Stop('PHYSICAL_MODEL_OR_SYNTAX_DRIFT_NO_UNREVIEWED_REUSE')
        snap=read(self.directory/'readiness-diagnostic-01/source-snapshot.json')
        for row in snap:
            p=Path(row['path'])
            if not p.is_absolute():p=PROJECT/p
            if p.suffix=='.mo' and sha(p)!=row['sha256']:
                raise Stop('FRONTEND_DEPENDENCY_DRIFT')
        return policy.validate_frontend(binding.get('frontend_reuse'),sha(package),
            manifest['model_class_by_scene'],expected,read(result_path))

    def reuse_sysml(self,sysml,binding):
        self.assert_sources(binding)
        old=self.base['source_bindings'][-1]
        entry=next(r for r in old['files'] if r['path'].endswith('/candidate.sysml'))
        if sha(PROJECT/entry['path'])!=entry['sha256'] or sha(sysml)!=entry['sha256']:
            raise Stop('SYSML_ORIGINAL_HASH_AND_EXACT_REUSE_REQUIRED')
        parsed=self.base['parse_attempts'][-1]
        if parsed['status']!='PASS' or parsed['round']!=3 or parsed['report']['tool_version']!='0.34.1':
            raise Stop('NO_PINNED_ACTUAL_PARSE_EVIDENCE')
        return True

    def validate_admission(self):
        binding=self.data[KEY].get('binding')
        if not binding:raise Stop('NO_READINESS_BINDING')
        self.assert_sources(binding)
        binding_file=Path(binding['generated']['directory'])/'source-binding.json'
        if read(binding_file)!=binding:raise Stop('BOUND_METADATA_DRIFT')
        admit=read(self.directory/'execution-remediation-admission.json')
        policy.validate_admission(admit,sha(binding_file),
            sha(self.directory/admit['readiness_result']),sha(self.directory/admit['independent_review']))

    def terminal(self,reason,status='STOPPED'):
        self.refresh();self.data[KEY].update(status='STOPPED_AFFECTED_EXECUTION',stop_reason=reason)
        self.data.update(status='STOPPED_REMEDIATION',stop_reason=reason);self.persist()

    def end_parent(self,status,error=''):
        self.refresh();row=self.data[KEY]['launchers'][-1]
        overhead=max(0,time.monotonic()-self.parent_started-(self.scene_leaf()-row['scene_leaf_before']))
        row.update(status='RETURNED',result=status,error=error,ended_utc=stamp(),process_parent_s=overhead)
        if overhead>60:self.data[KEY].update(status='STOPPED_TIME',stop_reason='PARENT_OVERHEAD_LIMIT')
        self.persist()
