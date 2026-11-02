"""Independent whole-route checks over raw CSV; no hardware certification."""
import csv
import hashlib
import math
from statistics import median
from pathlib import Path
from .case_scope import modelica_parameters

REQUIRED = ('x','z','v','vz','psi','omega_psi','x_n','z_n','x_f','z_f','x_r','z_r','h_n','h_f','h_r','Fz_n','Fz_f','Fz_r')
LIMITATIONS = ['三个纵向等效接点，非六个独立车轮。','原模型没有完整立面/棱线接触、车架碰撞或真实机电耦合；本次只作旧方程诊断，不能验收物理越障能力。','整圆严格处于统一楼梯实体内部是采样几何异常；普通轮心入实体、轮缘相交及软接触压缩另列观测，不设穿透百分比阈值。','有限采样未见严重异常不等于连续接触正确或可信物理PASS。']
REVIEWED_CONTACT_FAMILY = 'source-review-bound-stair-contact-v1'
REVIEWED_LIMITATIONS = [
    '三个纵向等效圆轮接点，非六个独立车轮；没有车架碰撞或真实机电耦合及硬件认证。',
    '已审核源码补入统一踏面、立面和棱线接触；源码审核不替代连续碰撞、力学或实物验证。',
    LIMITATIONS[2], LIMITATIONS[3],
]
UNKNOWN_SOURCE_LIMITATIONS = [
    '当前源码尚无可信接触家族绑定；不能依据历史模型说明判断本源码是否包含立面或棱线接触。',
    LIMITATIONS[2], LIMITATIONS[3],
]


def _source_contact_support(source, values):
    """Select conventions from actual bound source, never a free-text scope."""
    from .case_contact_family import support_for_reviewed_source
    from .case_contact_validity import diagnose_contact_source
    support = support_for_reviewed_source(source)
    if support is not None:
        if support.get('family') != REVIEWED_CONTACT_FAMILY:
            raise ValueError('unrecognized registered contact family')
        return support
    legacy = diagnose_contact_source(source, values)
    if legacy.get('conclusion') == 'INCONSISTENT_TERRAIN_FORMULAS':
        return dict(family='inherited-split-height-formulas')
    return None

def terrain_binding(rows,values,source=None,*,_source_support=None):
    """Bind actual sampled terrain to this source's dimensions and conventions.

    This is an arithmetic evidence check, not a new penetration/safety gate.
    Historical sources retain the split formulas. Only a registered exact
    reviewed source uses the common solid; unknown source stays UNKNOWN.
    """
    support = _source_support if _source_support is not None else (_source_contact_support(source, values) if source is not None else dict(family='inherited-split-height-formulas'))
    if support is None:
        return dict(status='UNKNOWN',scope='UNRECOGNIZED_SOURCE_TERRAIN_CONVENTION',issues=['source has neither reviewed common terrain nor recognized inherited height formulas'])
    if support['family'] == REVIEWED_CONTACT_FAMILY:
        return _common_terrain_binding(rows, values, support)
    issues=[];maximum=0.;epsilon=1e-8
    for i,row in enumerate(rows):
        for entity in ('n','f','r'):
            x=row['x_'+entity];offset=1 if entity=='n' else 0
            if x<values['x_start_up']:expected=0.
            elif x<values['x_end_up']:
                steps=math.floor((x-values['x_start_up'])/values['stepD']+(0 if entity=='n' else 1e-3))+offset
                expected=steps*values['stepH']
            elif x<values['x_start_down']:expected=values['H_peak']
            elif x<values['x_end_down']:
                steps=math.floor((x-values['x_start_down'])/values['stepD']+(0 if entity=='n' else 1e-3))+offset
                expected=values['H_peak']-steps*values['stepH']
                if entity!='n':expected=max(0.,expected)
            else:expected=0.
            error=abs(row['h_'+entity]-expected);maximum=max(maximum,error)
            if error>epsilon and len(issues)<20:issues.append(dict(row=i,time=row['time'],entity=entity,x=x,expected=expected,actual=row['h_'+entity]))
    names=('stepH','stepD','n_steps','x_start_up','len_flat','x_end_up','x_start_down','x_end_down','H_peak')
    return dict(status='FAIL' if issues else 'PASS',parameters={k:values[k] for k in names},sample_count=len(rows),absolute_arithmetic_epsilon_m=epsilon,maximum_height_error_m=maximum,mismatches=issues,source_family=support['family'],scope='SOURCE_BOUND_SAMPLED_TERRAIN_NOT_CONTINUOUS_CONTACT_CERTIFICATION')


def _common_terrain_binding(rows, values, support):
    from .case_contact_validity import _geometry, _common_height
    p, epsilon, risers, _, _ = _geometry(values)
    positions = [r['x'] for r in risers]
    issues=[]; maximum=0.; boundary_samples=0
    for i,row in enumerate(rows):
        for entity in ('n','f','r'):
            x=row['x_'+entity]
            point_epsilon=max(epsilon,64*math.ulp(max(1.,abs(x))))
            lower,upper,at_riser=_common_height(x,risers,positions,point_epsilon)
            # At the exact vertical boundary either adjacent tread endpoint is
            # a valid height convention; an arbitrary intermediate h is not.
            expected=[lower,upper] if at_riser else [lower]
            error=min(abs(row['h_'+entity]-h) for h in expected)
            maximum=max(maximum,error);boundary_samples+=int(at_riser)
            if error>point_epsilon and len(issues)<20:
                issues.append(dict(row=i,time=row['time'],entity=entity,x=x,expected_endpoint_heights=expected,actual=row['h_'+entity]))
    return dict(status='FAIL' if issues else 'PASS',parameters=p,sample_count=len(rows),
                absolute_arithmetic_epsilon_m=epsilon,maximum_height_error_m=maximum,mismatches=issues,
                source_family=support['family'],source_review_sha256=support['source_review_sha256'],
                approved_package_sha256=support['approved_package_sha256'],same_solid_for_all_entities=True,
                exact_riser_endpoint_samples=boundary_samples,
                final_down_riser_x_m=p['x_start_down']+(int(p['n_steps'])-1)*p['stepD'],
                route_exit_x_m=p['x_end_down'],
                scope='REVIEWED_SOURCE_BOUND_COMMON_TERRAIN_SAMPLES_NOT_PHYSICAL_CERTIFICATION')

CONTACT_DOMAIN_REVIEWED_SOURCE_SHA256 = 'cfabd69f58616d0d4c7b1e6012e12a7596b961d9ce620a1081fd949af5d85c69'


def source_contact_domain(rows, values, support, *, actual_source_sha256, actual_source=None):
    """Finite domain of this exact reviewed formulation, never new authority.

    The source uses 100m ground pads and a 1e-9m corner regularization. These
    constants are valid only for its trusted registered shape; another source
    must not acquire them merely by claiming the same scope or family name.
    Geometry FAIL takes precedence at the assessment merge, not here.
    """
    from .case_contact_validity import _geometry, _number
    result=dict(schema='v12-reviewed-contact-domain-1',status='UNKNOWN',
                physical_status='UNKNOWN',scope='EXACT_REVIEWED_SOURCE_FINITE_CONTACT_DOMAIN',
                issues=[],reason_codes=[],space_violations=[],near_zero_convex_corners=[],
                counters=dict(row_count=len(rows),wheel_samples=0,space_violations=0,
                              near_zero_convex_corner_samples=0),
                limitations=['有限CSV采样不证明子采样状态始终满足接触域；完整物理状态仍UNKNOWN。'])
    def digest(value):
        return isinstance(value,str) and len(value)==64 and set(value)<=set('0123456789abcdef')
    from .case_contact_family import support_for_reviewed_source
    trusted = support_for_reviewed_source(actual_source) if isinstance(actual_source,str) else None
    domain = support.get('contact_domain') if isinstance(support,dict) else None
    if (not isinstance(support,dict) or support.get('family')!=REVIEWED_CONTACT_FAMILY
            or trusted is None or trusted!=support
            or not isinstance(domain,dict) or domain.get('schema')!='v12-reviewed-contact-domain-1'
            or domain.get('reference_source_sha256')!=CONTACT_DOMAIN_REVIEWED_SOURCE_SHA256
            or domain.get('reviewed_source_sha256')!=support.get('reviewed_modelica_source_sha256')
            or domain.get('family_shape_sha256')!=support.get('family_shape_sha256')
            or domain.get('geometry_unchanged') is not True
            or domain.get('ground_pad_extension_m')!=100.
            or domain.get('corner_regularization_distance_m')!=1e-9
            or not digest(actual_source_sha256)
            or hashlib.sha256(actual_source.encode('utf8')).hexdigest()!=actual_source_sha256
            or support.get('modelica_source_sha256')!=actual_source_sha256
            or any(not digest(support.get(name)) for name in ('reviewed_modelica_source_sha256','family_shape_sha256','source_review_sha256','approved_package_sha256'))):
        result['reason_codes'].append('CONTACT_DOMAIN_BINDING_UNKNOWN')
        result['issues'].append('接触域100m/1e-9规则缺少当前精确已审源码家族的可信绑定。')
        return result
    result['binding']={name:support[name] for name in ('family','modelica_source_sha256',
                      'reviewed_modelica_source_sha256','family_shape_sha256',
                      'source_review_sha256','approved_package_sha256')}
    result['binding']['contact_domain']=dict(domain)
    try:
        p,epsilon,risers,_,_=_geometry(values)
        left=p['x_start_up']-100.;right=p['x_end_down']+100.
        corners=[dict(x=r['x'],z=r['high'],phase=r['phase'],step=r['step']) for r in risers]
        result['limits']=dict(ground_pad_extension_m=100.,ground_left_x_m=left,
                              ground_right_x_m=right,corner_regularization_distance_m=1e-9,
                              boundary_arithmetic_epsilon_m=epsilon,
                              scenario_scope='THREE_APPROVED_H_D_POINTS_ENFORCED_BY_TRUSTED_FAMILY_ADAPTER')
        for i,row in enumerate(rows):
            time=_number(row.get('time'),'time')
            for entity in ('n','f','r'):
                x=_number(row.get('x_'+entity),'x_'+entity)
                z=_number(row.get('z_'+entity),'z_'+entity)
                radius=p['R_nose' if entity=='n' else 'R_main']
                result['counters']['wheel_samples']+=1
                if x-radius<left-epsilon or x+radius>right+epsilon:
                    result['counters']['space_violations']+=1
                    if len(result['space_violations'])<20:
                        result['space_violations'].append(dict(row=i,time_s=time,entity=entity,
                            wheel_left_x_m=x-radius,wheel_right_x_m=x+radius))
                for corner in corners:
                    # A convex endpoint owns only its exterior normal cone.
                    # At a concave root there are two face contacts instead.
                    in_cone=z>=corner['z'] and (x<=corner['x'] if corner['phase']=='up' else x>=corner['x'])
                    if not in_cone:continue
                    distance=math.hypot(x-corner['x'],z-corner['z'])
                    if distance<=1e-9 and distance<radius:
                        result['counters']['near_zero_convex_corner_samples']+=1
                        if len(result['near_zero_convex_corners'])<20:
                            result['near_zero_convex_corners'].append(dict(row=i,time_s=time,
                                entity=entity,phase=corner['phase'],step=corner['step'],
                                corner_x_m=corner['x'],corner_z_m=corner['z'],distance_m=distance,
                                radial_singularity=distance==0.))
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        result['reason_codes'].append('CONTACT_DOMAIN_EVIDENCE_UNKNOWN')
        result['issues'].append(str(exc));return result
    if result['counters']['space_violations']:
        result['reason_codes'].append('OUTSIDE_SOURCE_CONTACT_SPACE_DOMAIN')
        result['issues'].append('至少一个轮圆越过已审源码有限平地两端；共同无限h(x)不能证明该处接触力一致。')
    if result['counters']['near_zero_convex_corner_samples']:
        result['reason_codes'].append('UNVERIFIED_CONTACT_NORMAL_REGULARIZATION')
        result['issues'].append('活动凸角接触进入已审源码dist<=1e-9的法向fallback或零距离奇异，尚无获批的力误差依据。')
    if not result['reason_codes']:result['status']='PASS'
    return result


def read_rows(csv_path):
    with Path(csv_path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle); columns = reader.fieldnames or []
        if len(columns) != len(set(columns)) or any(n not in columns for n in ('time', *REQUIRED)): raise ValueError('missing/duplicate required mission CSV fields')
        optional=tuple(k for k in ('Fx_n','Fx_f','Fx_r') if k in columns)
        rows = [{k: float(row[k]) for k in ('time', *REQUIRED, *optional)} for row in reader]
    if len(rows) < 3 or any(not math.isfinite(v) for row in rows for v in row.values()): raise ValueError('nonfinite/insufficient mission evidence')
    if any(b['time'] < a['time'] for a,b in zip(rows, rows[1:])) or rows[-1]['time'] <= rows[0]['time']: raise ValueError('solver time did not advance monotonically')
    return rows

def assess_route_rows(rows, values, *, source_family=None):
    """Inherited route primitive only; never use alone as physical validity."""
    limitations=REVIEWED_LIMITATIONS if source_family==REVIEWED_CONTACT_FAMILY else LIMITATIONS
    result = dict(schema='v12-inherited-mission-check-1',task_status='UNKNOWN',validity_status='UNKNOWN',scope='SIMPLIFIED_MODEL_ROUTE_ONLY' if source_family==REVIEWED_CONTACT_FAMILY else 'INHERITED_SIMPLIFIED_MODEL_ROUTE_ONLY',limitations=list(limitations),issues=[],events=[],completion_time=None)
    if len(rows) < 3 or any(not math.isfinite(v) for r in rows for v in r.values()):
        result['issues'].append('nonfinite/insufficient state evidence'); return result
    if any(b['time'] < a['time'] for a,b in zip(rows, rows[1:])) or rows[-1]['time'] <= rows[0]['time']:
        result['issues'].append('time did not advance'); return result
    # Explicit topology inversion, not a velocity/performance or compression threshold.
    inverted = [i for i,r in enumerate(rows) if r['x_n'] <= r['x_f'] or r['x_f'] <= r['x_r'] or math.cos(r['psi']) <= 0]
    result['topology_inversion_rows'] = inverted[:20]
    result['compression_observations'] = {label:dict(maximum_m=max(max(0.,r['h_'+label]+radius-r['z_'+label]) for r in rows),positive_sample_count=sum(r['h_'+label]+radius-r['z_'+label]>0 for r in rows)) for label,radius in [('n',values['R_nose']),('f',values['R_main']),('r',values['R_main'])]}
    result['time_start'] = rows[0]['time']; result['time_end'] = rows[-1]['time']; result['sample_count'] = len(rows)
    if inverted:
        result.update(validity_status='FAIL',task_status='FAIL'); result['issues'].append('front/rear longitudinal ordering inverted or model overturned'); return result
    result['validity_status'] = 'PASS'
    # Inspect actual terrain variables too: the inherited nose and main-wheel
    # floor formulas have different step-edge conventions and must stay distinct.
    terrain_events=[];terrain_order=[];level_epsilon=1e-9
    for phase in ('up','down'):
        for step in range(1,int(values['n_steps'])+1):
            height=step*values['stepH'] if phase=='up' else (int(values['n_steps'])-step)*values['stepH']
            group=[]
            for entity in ('n','f','r'):
                found=None
                for i,r in enumerate(rows):
                    in_phase=r['x_'+entity]<values['x_start_down'] if phase=='up' else r['x_'+entity]>=values['x_start_down']
                    crossed=r['h_'+entity]>=height-level_epsilon if phase=='up' else r['h_'+entity]<=height+level_epsilon
                    if in_phase and crossed:
                        found=dict(phase=phase,step=step,entity=entity,height=height,index=i,time=r['time'],time_bracket=[rows[max(0,i-1)]['time'],r['time']]);break
                if found is None:
                    result.update(task_status='FAIL',completion_status='OBSERVATION_INTERVAL_INCOMPLETE',terrain_events=terrain_events);result['issues'].append('actual terrain '+phase+' level '+str(step)+' not reached by '+entity);return result
                terrain_events.append(found);group.append(found)
            if not group[0]['index']<=group[1]['index']<=group[2]['index']:
                result.update(task_status='FAIL',completion_status='TERRAIN_EVENT_ORDER_FAIL',terrain_events=terrain_events);result['issues'].append('actual terrain nose/front/rear event order differs');return result
            terrain_order.append([phase,step,'n','f','r'])
    result['terrain_events']=terrain_events;result['terrain_event_order_signature']=terrain_order
    cursors = {e:0 for e in ('n','f','r')}; event_orders = []
    for phase,start in [('up',values['x_start_up']),('down',values['x_start_down'])]:
        for step in range(int(values['n_steps'])):
            boundary = start + step * values['stepD']; group = []
            for entity in ('n','f','r'):
                key = 'x_'+entity; found = None
                for i in range(max(1,cursors[entity]),len(rows)):
                    left,right = rows[i-1],rows[i]
                    if left[key] < boundary <= right[key]:
                        found = dict(phase=phase,step=step+1,entity=entity,boundary=boundary,index=i,time_bracket=[left['time'],right['time']],time=right['time'],direction='FORWARD')
                        cursors[entity] = i; break
                if found is None:
                    result.update(task_status='FAIL',completion_status='OBSERVATION_INTERVAL_INCOMPLETE'); result['issues'].append(phase+' step '+str(step+1)+' not crossed forward by '+entity); return result
                group.append(found); result['events'].append(found)
            if not (group[0]['index'] <= group[1]['index'] <= group[2]['index']):
                result.update(task_status='FAIL',completion_status='EVENT_ORDER_FAIL'); result['issues'].append('nose/front/rear step order differs'); return result
            event_orders.append([phase,step+1,'n','f','r'])
    radius = values['R_main']; exit_x = values['x_end_down']
    # All represented wheel rear edges must leave the exit. Multiple later times
    # must still be beyond it and show net forward travel; local rebound is allowed.
    exits = [i for i,r in enumerate(rows) if r['x_r']-radius > exit_x and r['x_f']-radius > exit_x and r['x_n']-values['R_nose'] > exit_x]
    complete = None
    for i in exits:
        later = [r for r in rows[i+1:] if r['time'] > rows[i]['time']]
        if len({r['time'] for r in later}) >= 3 and all(r['x_r']-radius > exit_x for r in later) and later[-1]['x_r'] > rows[i]['x_r']:
            complete = i; break
    result['event_order_signature'] = event_orders
    if complete is None:
        result.update(task_status='FAIL',completion_status='OBSERVATION_INTERVAL_INCOMPLETE'); result['issues'].append('no complete wheel-edge exit with continued forward samples'); return result
    result.update(task_status='PASS',completion_status='COMPLETE_FORWARD_UP20_DOWN20_EXIT_AND_CONTINUE',completion_time=rows[complete]['time'],completion_row=complete,exit_x=exit_x,rear_edge_at_completion=rows[complete]['x_r']-radius,post_exit_distinct_times=len({r['time'] for r in rows[complete+1:]}),minimum_down_pitch=min(r['psi'] for r in rows if values['x_start_down'] < r['x_f'] < values['x_end_down']))
    # Front/nose-first forward descent with preserved front/rear ordering is the
    # approved head-down task interpretation; report pitch separately, no new angle cap.
    result['descent_orientation'] = 'FORWARD_NOSE_FRONT_REAR_ORDER'
    return result

def body_motion_observations(rows, values):
    """Observe body lift/return and pose; no new performance acceptance gate."""
    result=dict(schema='v12-body-route-observations-1',scope='SAMPLED_KINEMATICS_ONLY',
                status='UNKNOWN',physical_status='UNKNOWN',acceptance_thresholds=None,
                limitations=['高度、姿态及角速度仅为观测；缺少某阶段样本仅使该指标未知，不使路线失败。'])
    keys=('time','x','z','psi','omega_psi','x_n','x_f','x_r')
    if not rows or any(any(type(r.get(k)) not in (int,float) or not math.isfinite(r[k]) for k in keys) for r in rows):
        result['issues']=['车体运动观测字段不足或非有限。'];return result
    radii={'n':values['R_nose'],'f':values['R_main'],'r':values['R_main']}
    plateau_left=values['x_end_up']
    groups={name:[] for name in ('entry_ground','all_wheels_platform','all_wheels_exit')}
    for i,row in enumerate(rows):
        if all(row['x_'+e]+radii[e]<values['x_start_up'] for e in radii):groups['entry_ground'].append(i)
        if all(plateau_left<=row['x_'+e]-radii[e] and row['x_'+e]+radii[e]<=values['x_start_down'] for e in radii):groups['all_wheels_platform'].append(i)
        if all(row['x_'+e]-radii[e]>values['x_end_down'] for e in radii):groups['all_wheels_exit'].append(i)
    entry=groups['entry_ground'];reference=rows[entry[0]] if entry else None
    result['entry_reference']=dict(row=entry[0],time_s=reference['time'],x_m=reference['x'],z_m=reference['z']) if reference else None
    result['phase_geometry']=dict(platform_left_x_m=plateau_left,platform_right_x_m=values['x_start_down'],route_exit_x_m=values['x_end_down'],whole_circle_extents=True)
    def summary(indices):
        if not indices:return dict(status='UNKNOWN',sample_count=0,reason='NO_FULL_WHEEL_PHASE_SAMPLE')
        subset=[rows[i] for i in indices]
        return dict(status='OBSERVED',sample_count=len(indices),first_row=indices[0],last_row=indices[-1],
                    first_time_s=subset[0]['time'],last_time_s=subset[-1]['time'],
                    z_min_m=min(r['z'] for r in subset),z_max_m=max(r['z'] for r in subset),
                    z_median_m=median(r['z'] for r in subset),
                    z_first_m=subset[0]['z'],z_last_m=subset[-1]['z'],
                    z_relative_entry_range_m=[min(r['z'] for r in subset)-reference['z'],max(r['z'] for r in subset)-reference['z']] if reference else None,
                    psi_range_rad=[min(r['psi'] for r in subset),max(r['psi'] for r in subset)],
                    omega_psi_range_rad_s=[min(r['omega_psi'] for r in subset),max(r['omega_psi'] for r in subset)])
    result['phases']={name:summary(indices) for name,indices in groups.items()}
    entry_median=median(rows[i]['z'] for i in entry) if entry else None
    platform_median=median(rows[i]['z'] for i in groups['all_wheels_platform']) if groups['all_wheels_platform'] else None
    result['platform_median_z_relative_entry_median_m']=platform_median-entry_median if platform_median is not None and entry_median is not None else None
    result['whole_observation']=summary(list(range(len(rows))))
    platform=groups['all_wheels_platform'];exit_rows=groups['all_wheels_exit']
    result['platform_to_exit_drop_m']=rows[platform[-1]]['z']-rows[exit_rows[-1]]['z'] if platform and exit_rows else None
    result['final_z_relative_entry_m']=rows[-1]['z']-reference['z'] if reference else None
    result['status']='OBSERVED';return result


def assess_rows(rows, values, *, source_family=None):
    """Keep route completion separate from an independent contact diagnosis."""
    from .case_contact_validity import diagnose_contact_rows
    route=assess_route_rows(rows,values,source_family=source_family)
    contact=diagnose_contact_rows(rows,values,source_family=source_family)
    result=dict(route,route_status=route['task_status'],route_assessment=dict(route),
                diagnostic_status=contact['status'],contact_validity=contact,physical_status='UNKNOWN',
                assessment_evidence_status='PASS' if route.get('validity_status')=='PASS' and contact['status'] in ('PASS','FAIL') else 'UNKNOWN',
                body_motion_observations=body_motion_observations(rows,values))
    result['issues']=list(route.get('issues',[]))+list(contact.get('issues',[]))
    # Route completion and sampled contact diagnosis are independent facts.
    # UNKNOWN evidence still prevents a trusted task PASS; a known anomaly is
    # reported explicitly without deleting observed route completion.
    if route.get('validity_status')=='FAIL':
        result.update(task_status='UNKNOWN',validity_status='FAIL',completion_status='ROUTE_STATE_INVALID')
    elif contact['status']=='UNKNOWN':
        result.update(task_status='UNKNOWN',validity_status='UNKNOWN',completion_status='CONTACT_EVIDENCE_UNKNOWN')
    elif contact['status']=='FAIL':
        result.update(task_status='UNKNOWN',validity_status='FAIL',completion_status='CONTACT_ANOMALY_ROUTE_COMPLETE' if route['task_status']=='PASS' else 'CONTACT_ANOMALY')
    else:result['validity_status']=contact['status']
    result['overall_summary']='ROUTE_COMPLETE_CONTACT_ANOMALY' if result['route_status']=='PASS' and contact['status']=='FAIL' else 'ROUTE_COMPLETE_LIMITED_DIAGNOSTIC' if result['task_status']=='PASS' else 'ROUTE_NOT_COMPLETE' if result['task_status']=='FAIL' else 'EVIDENCE_UNKNOWN'
    return result

def assess(spec, csv_path):
    limitations=UNKNOWN_SOURCE_LIMITATIONS
    try:
        source=spec['artifact']['modelica_source']
        values,_,_=modelica_parameters(source)
        support_error=None
        try:support=_source_contact_support(source,values)
        except (ValueError,KeyError,TypeError) as exc:support=None;support_error=str(exc)
        limitations=REVIEWED_LIMITATIONS if support and support.get('family')==REVIEWED_CONTACT_FAMILY else LIMITATIONS if support else UNKNOWN_SOURCE_LIMITATIONS
        rows=read_rows(csv_path);stop=spec['settings']['stop_time'];epsilon=max(1e-12,abs(stop)*1e-9)
        observation_issues=[]
        if abs(rows[0]['time'])>epsilon or abs(rows[-1]['time']-stop)>epsilon:observation_issues.append('CSV does not cover the bound observation start/end')
        interval=stop/spec['settings']['intervals']
        if any(b['time']-a['time']>interval+epsilon for a,b in zip(rows,rows[1:])):observation_issues.append('CSV gap exceeds bound output observation interval')
        binding=dict(status='UNKNOWN',scope='CONTACT_SOURCE_BINDING_UNKNOWN',issues=[support_error]) if support_error else terrain_binding(rows,values,_source_support=support,source=source)
        result=assess_rows(rows,values,source_family=support['family'] if support else None);result['terrain_binding']=binding
        if binding['status']!='PASS':
            if binding['status']=='FAIL':
                result.update(task_status='UNKNOWN',validity_status='FAIL',completion_status='INVALID_MODEL')
                result['issues'].append('sampled terrain differs from source-bound stair dimensions or recognized source conventions')
            elif result.get('validity_status')!='FAIL':
                result.update(task_status='UNKNOWN',validity_status='UNKNOWN',completion_status='CONTACT_EVIDENCE_UNKNOWN')
                result['issues'].extend(binding.get('issues',[]))
        if support and support.get('family')==REVIEWED_CONTACT_FAMILY:
            domain=source_contact_domain(rows,values,support,actual_source_sha256=hashlib.sha256(source.encode('utf8')).hexdigest(),actual_source=source)
            result['contact_source_domain']=domain
            if domain['status']!='PASS':
                result['issues'].extend(domain.get('issues',[]))
                # A pre-existing rigid geometry/terrain FAIL is never demoted.
                if result.get('validity_status')!='FAIL':
                    result.update(task_status='UNKNOWN',validity_status='UNKNOWN',completion_status='CONTACT_DOMAIN_UNKNOWN')
        result['observation_binding']=dict(start_time=0,stop_time=stop,output_interval_s=interval,endpoint_epsilon=epsilon,status='FAIL' if observation_issues else 'PASS',issues=observation_issues)
        if observation_issues:
            result['issues'].extend(observation_issues)
            result['task_status']='UNKNOWN'
            if result.get('validity_status')!='FAIL':result.update(validity_status='UNKNOWN',completion_status='CSV_OBSERVATION_INCOMPLETE')
        gates=[binding['status'],result['observation_binding']['status']]
        if 'contact_source_domain' in result:gates.append(result['contact_source_domain']['status'])
        if any(status!='PASS' for status in gates):
            result['assessment_evidence_status']='UNKNOWN';result['task_status']='UNKNOWN'
            if result.get('completion_status') in ('CONTACT_ANOMALY_ROUTE_COMPLETE','CONTACT_ANOMALY'):result['completion_status']='EXECUTION_EVIDENCE_INCOMPLETE'
        if result['task_status']=='UNKNOWN' and result.get('completion_status') not in ('CONTACT_ANOMALY_ROUTE_COMPLETE','CONTACT_ANOMALY'):result['overall_summary']='EVIDENCE_UNKNOWN'
        return result
    except (OSError,ValueError,KeyError,TypeError) as exc: return dict(schema='v12-inherited-mission-check-1',task_status='UNKNOWN',validity_status='UNKNOWN',physical_status='UNKNOWN',issues=[str(exc)],limitations=list(limitations))

def compare_numerics(first, refined):
    ordered=first.get('event_order_signature') == refined.get('event_order_signature') and first.get('terrain_event_order_signature') == refined.get('terrain_event_order_signature')
    stable = first.get('task_status') == refined.get('task_status') and ordered
    a,b = first.get('completion_time'),refined.get('completion_time')
    return dict(status='PASS' if stable and first.get('task_status')=='PASS' else 'NUMERICALLY_SENSITIVE' if not stable else 'NOT_ACCEPTED',task_conclusion_consistent=first.get('task_status')==refined.get('task_status'),event_order_consistent=ordered,completion_time_difference_s=b-a if a is not None and b is not None else None,time_difference_acceptance_threshold=None)
