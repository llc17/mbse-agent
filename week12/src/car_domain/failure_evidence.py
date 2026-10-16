"""Bounded independent failure evidence, with conservative UNKNOWN exits.

This never changes the full-task PASS verifier. Only a geometrical failure with
complete trusted physical outputs has a supported numerical failure predicate.
"""
from pathlib import Path
import csv
import math
from week12.src.active_cluster.verify import _frames, _normalize_scene, _bind_csv_graph, _frozen_hardware, _shape_clearance

MODEL_MARKERS=('balance','closure','torque_balance','inertia','finite_','energy_sum','load_sum')

def eligible_failure(result,report):
    physical=report.get('physical',{})
    checks=physical.get('checks',[])
    if result.get('variant')!='base': return False
    if result.get('compile')!='PASS' or result.get('simulation')!='COMPLETED' or result.get('verification_returncode') not in (0,2): return False
    if result.get('verification_timed_out') or result.get('verification_uncertain'): return False
    if not checks or any(c.get('status')=='REQUIRES_EVIDENCE' for c in checks): return False
    failed=[c['code'] for c in checks if c.get('status')=='FAIL']
    if any(any(mark in c for mark in MODEL_MARKERS) for c in failed): return False
    return any(c.startswith('actual_hardware_5mm_') for c in failed)

class _Collector:
    def add(self,*args,**kwargs): pass
    def missing(self,*args,**kwargs): raise ValueError('failure predicate binding missing')

def _predicates(contract,scene,spec,graph):
    local,info=_normalize_scene(contract,scene,spec,graph)
    local=dict(local)
    for key in ('bodies','joints','drives','wheels','hardware'):
        if key in info: local[key]=info[key]
    bound=_bind_csv_graph(_Collector(),graph,local,scene)
    ids={b['id'] for b in local['bodies']}
    if local.get('_fixture'):
        ids.discard('chassis')
        bound=dict(bound,joints=[dict(j,mount=[0,0,0]) if j['kind']=='active_revolute' else j for j in bound['joints']])
    if local['wheels']: local['hardware']=_frozen_hardware(spec,bound,ids)
    bodies={b['id']:b for b in local['bodies']}
    return local,info,bodies

def _summary(path,local,info,bodies):
    shapes=local.get('hardware',[])
    columns={key for b in bodies.values() for key in b['columns'].values()}
    energies={k:local['totals'][k] for k in ()} # no assumed totals schema
    rawcols=info.get('columns',{})
    # Normalized totals are explicit columns in the supported graph-generated contract.
    for key in ('battery_energy','dump_energy','mechanical_energy','shaft_energy','contact_loss_energy','loss_energy'):
        column=local.get('totals',{}).get(key) or rawcols.get(key) or local.get('columns',{}).get(key)
        if isinstance(column,str): energies[key]=column
    if not energies: raise ValueError('failure numerical energy fields unavailable')
    columns|=set(energies.values())|{'time'}
    minima={};first={};last=None;count=0;first_time=None;last_time=None
    for frame in _frames(Path(path),columns):
        t=frame['time'];first_time=t if first_time is None else first_time
        for shape in shapes:
            b=bodies[shape['body_id']];pose=tuple(frame[b['columns'][k]] for k in ('x','z','phi'))
            value=_shape_clearance(shape,pose,info['terrain']['points'])
            code='actual_hardware_5mm_'+shape['id']
            minima[code]=min(minima.get(code,math.inf),value)
            if value<.005-1e-8: first.setdefault(code,t)
        last=frame;last_time=t;count+=1
    if count<2 or first_time!=0 or last_time<=0: raise ValueError('no complete finite actual prefix')
    return dict(minima=minima,first=first,last=last,first_time=first_time,last_time=last_time,energies=energies,count=count)

def check_failure_pair(base_csv,refined_csv,contract,scene,spec,graph,base_physical,refined_physical):
    answer={'status':'UNKNOWN','scope':'specific failure evidence only, never task PASS','checks':[]}
    try:
        for physical in (base_physical,refined_physical):
            checks=physical.get('checks',[])
            if not checks or any(c.get('status')=='REQUIRES_EVIDENCE' for c in checks): raise ValueError('physical evidence incomplete')
            if any(c.get('status')=='FAIL' and any(m in c['code'] for m in MODEL_MARKERS) for c in checks): raise ValueError('model conservation/closure/finite gate failed')
        local,info,bodies=_predicates(contract,scene,spec,graph)
        a,b=(_summary(path,local,info,bodies) for path in (base_csv,refined_csv))
        if abs(a['last_time']-b['last_time'])>.25: raise ValueError('actual common coverage not equivalent')
        for body in bodies.values():
            for key in ('x','z'):
                col=body['columns'][key]
                if abs(a['last'][col]-b['last'][col])>.005: raise ValueError('failure state position numerical bound')
        for key,col in a['energies'].items():
            x,y=a['last'][col],b['last'][col]
            if key=='battery_energy': x,y=2764800-x,2764800-y
            if abs(x-y)>.01*max(abs(x),abs(y),1e-8): raise ValueError('failure energy numerical bound')
        selected=[]
        for code in sorted(set(a['first'])&set(b['first'])):
            x,y=a['minima'][code],b['minima'][code]
            error=abs(x-y)
            margin=.005-max(x,y)
            ok=error<=.005 and margin>max(error,1e-8) and abs(a['first'][code]-b['first'][code])<=.25
            answer['checks'].append({'code':code,'status':'PASS' if ok else 'UNKNOWN','base_clearance_m':x,'refined_clearance_m':y,'predicate_difference_m':error,'failure_margin_m':margin,'event_difference_s':abs(a['first'][code]-b['first'][code])})
            if ok:selected.append(code)
        if not selected: raise ValueError('specific failure margin or repeated actual failure not established')
        answer.update(status='TRUSTED_FAILURE_EVIDENCE',specific_failed_predicates=selected,
            actual_common_prefix_end_s=min(a['last_time'],b['last_time']),not_a_complete_mission_proof=True,
            numerical_scope='same fixed task at base/refined settings; predicates plus finite physical/energy/closure evidence')
    except (KeyError,TypeError,ValueError,OSError,StopIteration) as exc:
        answer['reason']=str(exc)
    return answer
