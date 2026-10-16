"""Sanity checker for exactly the two generated C proposals, not arbitrary inputs.

It does not replace a future general physical schema or runtime acceptance.
"""
from copy import deepcopy
import math
from week12.src.composition.schema import require_valid, load_library, digest
from week12.src.component_physics.contract import terrain_segments, world_port


def validate_proposal(bundle):
    d,p=bundle['design'],bundle['physical']
    require_valid(d,load_library())
    if p['design_digest']!=d['design_digest']:
        raise ValueError('STALE design-to-physical binding')
    raw=deepcopy(p); old=raw.pop('physical_digest')
    if digest(raw)!=old:
        raise ValueError('STALE physical description')
    allc=[*d['bodies'],*d['wheels'],*d['drives'],d['power_supply']]
    items={c['id']:c for c in allc}
    mass=sum(c['count']*c['mass']['value'] for c in allc)
    if abs(mass-85)>1e-9 or p['mass_kg']!=85 or p['vehicle_without_payload_kg']!=65:
        raise ValueError('Mass must total85 including20 equipment')
    payload=next(c for c in d['bodies'] if c['role']=='payload')
    if payload['mass']['value']!=20:
        raise ValueError('Equipment mass must be20')
    if sum(c['count'] for c in d['wheels'])!=6 or not all(c['driven'] for c in d['wheels']):
        raise ValueError('Concrete proposals require all six driven wheels')
    # Check clearances from the actual shapes, not the assumed body COM.
    min_clear=math.inf
    bounds=[]
    for cid,shapes in p['envelopes_local_xz'].items():
        c=items[cid]; cp=tuple(c['pose'][k]['value'] for k in ('x','z','pitch'))
        for shape in shapes:
            points=shape['vertices'] if shape['type']=='polygon' else [shape['start'],shape['end']]
            radius=shape.get('radius',0)
            for point in points:
                x,z,_=world_port(cp,(*point,0))
                min_clear=min(min_clear,z-radius)
                bounds.extend([(x-radius,z-radius),(x+radius,z+radius)])
    required_envelopes={c['id'] for c in [*d['bodies'],*d['drives'],d['power_supply']]}
    if set(p['envelopes_local_xz'])!=required_envelopes:
        raise ValueError('Missing or extraneous nonwheel envelope')
    if min_clear<0:
        raise ValueError('Initial nonwheel body below flat terrain')
    for w in d['wheels']:
        x,z=(w['pose'][k]['value'] for k in ('x','z')); r=w['radius']['value']
        if z-r < -1e-9:
            raise ValueError('Initial wheel below flat terrain')
        bounds.extend([(x-r,z-r),(x+r,z+r)])
    for i,w in enumerate(d['wheels']):
        for v in d['wheels'][i+1:]:
            distance=math.hypot(w['pose']['x']['value']-v['pose']['x']['value'],w['pose']['z']['value']-v['pose']['z']['value'])
            if distance < w['radius']['value']+v['radius']['value']-1e-9:
                raise ValueError('Overlapping same-side wheel disks')
    total_rating=sum(c['count']*c['power']['value']/c['efficiency']['value'] for c in d['drives'])+p['supply']['auxiliary_W']
    if total_rating>p['supply']['continuous_power_W'] or p['supply']['continuous_power_W'] != p['supply']['voltage_V']*p['supply']['continuous_current_A']:
        raise ValueError('Aggregate continuous supply incompatible with all drive ratings')
    heights={}
    if any(j['id']=='rocker_pivot' for j in d['joints']):
        # Chassis plus its rigid payload/electronics/battery hang below rocker pivot.
        sprung=[c for c in allc if c['id'] in {'chassis','payload','controller','battery'}]
        sprung_m=sum(c['count']*c['mass']['value'] for c in sprung)
        z=sum(c['count']*c['mass']['value']*c['pose']['z']['value'] for c in sprung)/sprung_m
        pivot=next(j for j in d['joints'] if j['id']=='rocker_pivot')['pose']['z']['value']
        if z>=pivot:
            raise ValueError('Sprung centre of mass not below free rocker pivot')
        heights={'sprung_com_z_m':z,'rocker_pivot_z_m':pivot,'hanging_margin_m':pivot-z}
    seen=set()
    for case in p['cases']:
        if case['id'] in seen:
            raise ValueError('Duplicate scenario')
        seen.add(case['id'])
        terrain_segments(case['terrain_points'])
        if (case['riser_m'],case['tread_m'])!=(.15,.28) or case['steps'] not in (0,3,20):
            raise ValueError('Wrong confirmed stair geometry')
        if case['direction']=='descent' and case['initial_vertical_offset_m']!=case['steps']*.15+.005:
            raise ValueError('Wrong descending entry elevation')
        if case['reference_speed_table_s_m_s'][-1][0]!=case['stop_time_s']:
            raise ValueError('Reference schedule does not cover whole run')
    if len(seen)!=6:
        raise ValueError('Expected six explicit car cases per structure')
    from week12.src.integration.proposals import proposal
    name=d['design_id'].removeprefix('c_')
    expected=proposal(name)
    if bundle['binding']!=expected['binding']:
        raise ValueError('STALE or inconsistent binding preview')
    if d!=expected['design'] or p!=expected['physical']:
        raise ValueError('FROZEN_PROPOSAL_ONLY: changed fields or rehashed input require a new explicit proposal')
    return {'status':'PASS_STATIC_ONLY','loaded_mass_kg':mass,'nonwheel_initial_min_clearance_m':min_clear,
            'initial_dimensions_m':{'length':max(x for x,z in bounds)-min(x for x,z in bounds),
                                    'height':max(z for x,z in bounds)-min(z for x,z in bounds),
                                    'width_assumption':p['width_m']},
            'all_drive_rated_electrical_plus_aux_W':total_rating,
            'shared_supply_power_margin_W':p['supply']['continuous_power_W']-total_rating,
            **heights,'limits':['No runtime clearance or self-collision proof',
                               'Initial geometry, mass and rating consistency only; no dynamic equilibrium or stair pass claimed']}


def verification_contract():
    return {'schema':'v11-c-verification-1','status':'PROPOSED_NOT_EXECUTION_AUTHORIZATION',
        'numerical':{'base':{'solver':'dassl','tolerance':1e-6,'max_step_s':.001,'output_interval_s':.001},
                     'refined':{'solver':'dassl','tolerance':1e-7,'max_step_s':.0005,'output_interval_s':.001},
                     'keep_event_rows':True},
        'physical':{'normal_tension_tolerance_N':1e-8,'negative_loss_tolerance_W':1e-8,
                    'wheel_penetration_limit':'min(0.02m,0.15*individual_radius)',
                    'nonwheel_terrain_clearance_min_m':-.0001,
                    'energy_scale':'max(1J, integral(abs(total_shaft_power))dt, sum(m_i*g*(max(z_i)-min(z_i))))',
                    'mechanical_energy_residual_fraction':.01,'electrical_energy_residual_fraction':.005,
                    'electrical_scale':'max(1J, integral(total_dc_power)dt)',
                    'settled_window_s':[2,3], 'settled_vertical_speed_max_m_s':.01,'settled_pitch_rate_max_rad_s':.01,
                    'settled_force_residual_fraction_of_weight':.02,
                    'settled_moment_residual_Nm':.02*85*9.81*.5,'moment_reference':'instantaneous total system COM, include all external force moments and terrain rolling couples',
                    'pitch_absolute_limit_deg':60,'passive_joint_relative_angle_limit_deg':60,
                    'cruise_exit_speed_window_m_s':[.36,.44],'exit_forward_distance_m':.5,
                    'exit_speed_window_s':2,'exit_rule':'all wheel disks and body envelope x_min beyond last full tread+0.5m; continue moving; no forced stop',
                    'front_first_rule':{'heading':'+x forward, no reverse descent',
                        'edge_crossing_order':{'fixed_nose':['nose','front','rear'],'rocker_bogie':['front','middle','rear']},
                        'actual_drop_order':{'fixed_nose':['front','rear'],'rocker_bogie':['front','middle','rear']},
                        'actual_drop_definition':'first centre height falls >=0.01m below its settled top-platform centre height, after its axle crosses first edge',
                        'required_contact_evidence':'each axle initially supported on upper platform must lose that feature, descend and acquire a lower feature; record wheel IDs and contact times',
                        'nose_exception':'fixed nose initially has0.12m clearance: record initially_airborne, not fabricated upper-platform liftoff; trace its edge crossing, vertical motion and later lower contacts separately',
                        'event_tie_tolerance_s':.002,'edge_crossing_alone_is_not_pass':True},
                    'brake_stop_window_s':[18,20],'brake_speed_max_m_s':.01,
                    'brake_wheel_speed_max_rad_s':.1,'brake_stop_distance_from_t12_max_m':1.,
                    'steady_energy_window_s':[15,35],'steady_speed_relative_error':.05,'steady_power_cv_max':.05,
                    'slip_high_threshold_m_s':.2,'slip_high_continuous_time_max_s':2,
                    'slip_rule':'while supported at Fn>5N; exceeding threshold for 2s is task failure; short climbing slip recorded',
                    'stagnation_window_s':5,'stagnation_progress_min_m':.05,'backslide_max_m':.1,
                    'stagnation_scope':'stair traversal after t=5 and before exit; flat_brake stop excluded',
                    'energy_balance_terms':'delta(K+Ug+Uelastic)+integral(contact_losses+rolling_loss+passive_joint_damping_loss)-integral(shaft_power)',
                    'power_current_energy_limits':'same frozen supply and motor ratings at every sample; no unbounded peak allowance'},
        'numerical_comparison':{'position_abs_m':.001,'velocity_abs_m_s':.001,'wheel_speed_abs_rad_s':.01,
                     'pitch_abs_rad':math.radians(.1),'state_relative':.01,'peak_contact_force_relative':.05,'peak_contact_force_abs_N':1.,
                     'event_time_abs_s':.002,'event_time_relative':0.,'energy_relative':.01,'energy_abs_J':.1,
                     'both_physical_and_task_must_pass':True,'criterion':'states use state_relative and named floors; peak force uses peak_contact_force_relative/abs; event time uses abs only; energy uses energy_relative/abs. Difference<=max(floor,relative*max(abs(base),abs(refined)))'},
        'budget_proposal':{'scene_attempts_per_complete_round':28,'maximum_rounds':3,'maximum_scene_attempts':84,
                          'build_timeout_s':180,'simulation_timeout_s':180,'postprocess_timeout_s':60,
                          'execution_total_wall_s':7200,'product_online_calls':0,
                          'new_local_execution_checks_limit':24,'new_local_timeout_s':10,
                          'sysml_parse_limit':6,'sysml_parse_timeout_s':30,
                          'identity_probe_limit':2,'identity_probe_timeout_s':20,
                          'sysml_parse_scope':'at most one parse per structure per source-repair round; unused probes are not run',
                          'note':'proposed only; complete affected-set reserve; no B credit or refund of failed attempts'},
        'probes':[
            {'id':'shared_supply_probe','stop_time_s':2,'input':'two prescribed +10rad/s shafts; pair torques1Nm and2Nm; eta0.8; aux5W; supply48V/50W; E0=100J',
             'expected':'shaft30W; branches37.5W; bus42.5W; I=42.5/48A; E(2)=15J; drive loss7.5W',
             'tolerance':'power/current max(1e-8 absolute,1e-6 relative); E error<=1e-5J'},
            {'id':'rolling_resistance_probe','stop_time_s':2,'input':'isolated rotor J=0.01; omega0=10rad/s; R0.1; imposed normal load10N; Crr0.05; omegaReg0.1; no other load',
             'expected':'log(sinh(omega/0.1))=log(sinh(100))-50*t; Ek+integral(loss)=0.5J; externally reacted moment; nonnegative loss',
             'tolerance':'omega error<=1e-4rad/s; energy residual<=5e-5J; local fixed-input check Fn0 implies Mrr0'}],
        'scope':'Two frozen development designs; component probes use imposed shaft/load ONLY, never imposed vehicle trajectories'}
