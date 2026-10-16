"""Single approved recovery scope; no repairs, retries or budget reset."""
from datetime import datetime, timezone
from pathlib import Path
from .execution import Route, Stop, ORDER, SPEC, EXPECTED_SPEC, PROJECT, read, sha, stamp

PROPOSAL_SHA = '2d9dd7e0dda72b27a8efc71550eeaa067a9dba32a93abc4150b4b679abfdf502'
CAPS = {'future_cap_s':23400,'implementation_analysis_cap_s':12600,
        'process_cap_s':10800,'other_check_cap':17,'repair_rounds_cap':3,
        'scene_cap':42,'parse_cap':3,'source_revision_round_cap':4}

def validate(proposal, auth, base, data):
    if (auth.get('status') != 'APPROVED' or auth.get('user_exact') != '确认'
            or auth.get('proposal_sha256') != PROPOSAL_SHA
            or auth.get('scope') != proposal['budget']):
        raise Stop('RECOVERY_RECEIPT_INVALID')
    if auth.get('baseline_ledger_sha256') != proposal['before_resume_ledger']['sha256']:
        raise Stop('RECOVERY_BASE_BINDING_INVALID')
    for key, value in CAPS.items():
        if data.get(key) != value or base.get(key) != value:
            raise Stop('RECOVERY_CAP_DRIFT')
    for key in ('scene_attempts','parse_attempts','other_checks','repairs',
                'source_bindings','active_intervals'):
        if data[key][:len(base[key])] != base[key]:
            raise Stop('RECOVERY_HISTORY_DRIFT')
    if len(data['repairs']) != 3 or len(data['parse_attempts']) != 2:
        raise Stop('NO_ADDITIONAL_REPAIR_OR_PARSE')
    if len(data['other_checks']) != 18 or data['generator_readiness_diagnostic'] != base['generator_readiness_diagnostic']:
        raise Stop('OLD_AUX_OR_DIAGNOSTIC_DRIFT')
    if data['started_utc'] != base['started_utc'] or data['prepare_reserved_s'] != 5400:
        raise Stop('ORIGINAL_TIME_ORIGIN_DRIFT')

def extra_process(data, parent_elapsed=0):
    diag=float(data['generator_readiness_diagnostic']['actual_external_s'])
    recovery=data['vehicle_recovery']
    if recovery.get('status') == 'DISPATCHING':
        old=float(recovery['old_process_before'])
        leaf=sum(float(p.get('duration_s',0)) for r in data['scene_attempts']
                 for p in r.get('processes',[]))
        overhead=max(0,parent_elapsed-(leaf-float(recovery['scene_leaf_before'])))
    else:
        overhead=float(recovery.get('parent_overhead_s',0))
    return diag+overhead

class RecoveryRoute(Route):
    def __init__(self):
        super().__init__()
        self.proposal=read(self.directory/'vehicle-recovery-proposal.json')
        self.auth=read(self.directory/'vehicle-recovery-authorization.json')
        self.base=read(self.directory/'ledger-before-vehicle-recovery.json')
        if (sha(self.directory/'vehicle-recovery-proposal.json') != PROPOSAL_SHA
            or sha(self.directory/'ledger-before-vehicle-recovery.json') != self.auth['baseline_ledger_sha256']
            or sha(SPEC) != EXPECTED_SPEC):
            raise Stop('RECOVERY_FILE_BINDING_DRIFT')
        self.guard()

    def guard(self):
        self.refresh()
        validate(self.proposal,self.auth,self.base,self.data)
        r=self.data['vehicle_recovery']
        if r['authorization_sha256'] != sha(self.directory/'vehicle-recovery-authorization.json'):
            raise Stop('RECOVERY_AUTHORIZATION_DRIFT')
        if len(r['attempts']) != 1 or r['attempts'][0]['status'] != 'LAUNCH_REQUESTED':
            raise Stop('ONE_RESERVED_LAUNCHER_REQUIRED')
        if len(self.data['scene_attempts']) > len(self.base['scene_attempts'])+14:
            raise Stop('RECOVERY_SCENE_LIMIT')
        if r['status'] not in ('LAUNCH_RESERVED','DISPATCHING'):
            raise Stop('RECOVERY_SCOPE_ALREADY_CLOSED')

    def parent_elapsed(self):
        r=self.data['vehicle_recovery']
        return max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(r['actual_started_utc'])).total_seconds()) if r.get('actual_started_utc') else 0

    def process_used(self):
        return super().process_used()+extra_process(self.data,self.parent_elapsed())

    def limits(self):
        self.guard()
        r=self.data['vehicle_recovery']
        active=(datetime.now(timezone.utc)-datetime.fromisoformat(r['started_utc'].replace('Z','+00:00'))).total_seconds()
        newprocess=self.process_used()-float(self.base['external_process_accounted_s'])
        if active-newprocess>=300 or active>=3540 or newprocess>=3240:
            raise Stop('RECOVERY_TOTAL_TIME_LIMIT')
        if extra_process(self.data,self.parent_elapsed())-7.4799357>=60:
            raise Stop('RECOVERY_PARENT_OVERHEAD_LIMIT')

    def assert_ready(self):
        self.limits()
        super().assert_ready()

    def reserve(self,kind,label,wall,round_no=1):
        if kind!='scene' or round_no!=4:
            raise Stop('NO_RECOVERY_AUX_PARSE_OR_OTHER_ROUND')
        self.guard()
        new=self.data['scene_attempts'][len(self.base['scene_attempts']):]
        expected=[n+'-'+v for n in ORDER for v in ('base','refined')]
        if len(new)>=14 or label!=expected[len(new)] or any(r['status']!='PASS' for r in new):
            raise Stop('RECOVERY_FIXED_ORDER_NO_RETRY')
        if sum(r['reserved_wall_s'] for r in new)+wall>3180:
            raise Stop('RECOVERY_SET_BUDGET')
        return super().reserve(kind,label,wall,round_no)

    def child(self,slot,command,cwd,wall,logs,label):
        self.limits()
        row=self.data[slot[0]][slot[1]]
        if len(row['processes'])>=3 or label not in ('build','simulation','verification'):
            raise Stop('RECOVERY_CHILD_LIMIT')
        return super().child(slot,command,cwd,wall,logs,label)

    def reuse_sysml(self,sysml,binding):
        self.assert_sources(binding)
        old=Path(self.base['source_bindings'][-1]['generated']['directory'])/'candidate.sysml'
        if Path(sysml).read_bytes()!=old.read_bytes() or not any(r['status']=='PASS' and r['round']==3 for r in self.base['parse_attempts']):
            raise Stop('SYSML_EXACT_REUSE_REQUIRED')
        self.data['vehicle_recovery']['sysml_reuse']={'status':'REUSED_EXACT_BYTES','sha256':sha(sysml),'actual_parse_round':3,'scope':'syntax/minimum structure only'}
        self.persist()
        return True

    def accept_implementation_repair(self,*args):
        raise Stop('NO_FOURTH_REPAIR')
