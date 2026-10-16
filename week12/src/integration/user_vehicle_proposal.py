"""User-geometry-first C completion draft. No execution or inherited approval.

Reuse the existing fixed-nose numeric draft as explicit, reviewable assumptions.
The previous two-vehicle packet is neither an input receipt nor current authority.
"""
from copy import deepcopy
import math

from week12.src.composition.schema import digest, seal_design, require_valid, load_library
from week12.src.integration.preview import binding_preview
from week12.src.integration.proposals import proposal as historical_numeric_draft
from week12.src.integration.proposal_checks import validate_proposal as check_historical_draft
from week12.src.integration.proposal_checks import verification_contract as historical_contract
from week12.src.integration.user_baseline import baseline_mapping
from week12.src.component_physics.contract import terrain_segments

VERSION = 'v11-c-user-vehicle-completion-1'


def proposal():
    mapping = baseline_mapping()
    seed = historical_numeric_draft('fixed_nose')
    # Reuse the measured-by-code static properties, without treating old approval
    # or the old two-vehicle execution scope as authority for the new proposal.
    prior_static = check_historical_draft(seed)
    d, p = deepcopy(seed['design']), deepcopy(seed['physical'])
    d['design_id'] = 'c_user_4w2n_full_mission'
    d['revision'] = 'physical_completion_1_pending'
    d['baseline_version_id'] = 'user_full_mission_source_mapped_20260924'
    d['provenance'] = {
        'origin': 'user_structure_plus_explicit_physical_completion',
        'reference': mapping['source']['path']+' sha256='+mapping['source']['sha256'],
        'scope': 'Pending user approval; no independent AI design claim; old two-vehicle packet superseded; fixed-nose numeric draft reused only as explicit assumptions'}
    for wheel in d['wheels']:
        wheel['source'] = 'user_structure_with_AI_mass_and_inertia_completion'
        wheel['radius']['source'] = 'user_supplied_full_mission_source'
        for value in wheel['pose'].values():
            value['source'] = 'derived_user_source_nominal_axle_geometry'
    for scenario in d['scenario'].values():
        scenario['platform_length']['source'] = 'user_selected_full_mission_source'
        scenario['speed']['source'] = 'user_supplied_full_mission_target'
        scenario['friction']['source'] = 'user_supplied_source_assumption_not_tire_measurement'
    for drive in d['drives']:
        drive['torque']['source'] = 'pending_interpretation_source_equivalent_pair_torque_divided_by_two'
        for value in drive['pose'].values():
            value['source'] = 'derived_user_source_nominal_axle_geometry'
    d = seal_design(d)
    # Original full mission starts 10mm above nominal tangency, not the prior
    # 5mm draft. Settle freely before commanding drive; never constrain pitch.
    p['initialization'].pop('global_vertical_lift_m')
    p['initialization'].pop('descent_vertical_offset')
    p['initialization']['release_clearance_m'] = .01
    p['initialization']['execution_field'] = 'case.initial_world_translation_z_m ONLY; already includes terrain start height plus release; never add release twice'
    p['initialization']['note'] = 'Source 10mm release; body reference changed by explicit mass layout; 3s free settling, no pose servo'
    for case in p['cases']:
        case.pop('initial_vertical_offset_m')
        case['initial_world_translation_z_m'] = (case['steps']*.15 if case['direction']=='descent' else 0)+.01
    full = mapping['full_mission']
    # .4m/s over 15.2m plus rear envelope/margin and 5s settling/ramp is
    # approximately 45s; 100s gives a finite crossing window with stair margin.
    p['cases'].append({
        'id': 'fixed_nose_full_mission', 'task': 'full_mission', 'direction': 'up_then_forward_down',
        'steps': 20, 'riser_m': .15, 'tread_m': .28, 'platform_length_m': 2.,
        'first_riser_x_m': 2., 'up_end_x_m': full['up_end_x_m'],
        'first_down_edge_x_m': full['down_start_x_m'],
        'exit_plane_x_m': full['down_end_x_m'], 'exit_margin_m': .5,
        # Extend purely numerical support past the full time window. Not a
        # requirement for a 30m real platform or autonomous navigation.
        'terrain_points': full['points_xz_m'][:-1]+[[full['down_end_x_m']+30., 0.]],
        'stop_time_s': 100, 'reference_speed_table_s_m_s': [[0,0],[3,0],[5,.4],[100,.4]],
        'initial_world_translation_z_m': .01, 'steady_window_s': None,
        'requires': ['fixed_nose_twenty_up','fixed_nose_twenty_down','fixed_nose_flat_energy'],
        'numeric_variants': ['base','refined'],
        'state_continuity': 'one solve; never reset pose, wheel speed, controller, contact or bus energy',
        'events': 'all20 up risers then platform then all20 down risers then all geometry beyond exit+0.5m'})
    p.pop('physical_digest')
    p['schema'] = VERSION
    p['design_digest'] = d['design_digest']
    p['source_mapping_digest'] = digest(mapping)
    p['source_model_sha256'] = mapping['source']['sha256']
    p['energy_reference_sha256'] = mapping['energy_reference']['sha256']
    p['original_terms_disposition'] = {
        'mass_85': 'preserved including20kg payload',
        'I_pitch_20': 'source lumped reference; NOT retained; new value from complete parts ledger',
        'original_COM_at_main_axle': 'NOT retained; source wheel geometry preserved, COM recomputed',
        'ground_40000_3000': 'NOT retained; B pair100000/200 proposed pending full-car validation',
        'translational_50v': 'removed; proposed external rolling resistance Crr0.05 with explicit loss',
        'pitch_300omega': 'removed; no artificial world-to-chassis pitch damper',
        'noEvent_height_functions': 'replaced with common finite terrain and B event-aware contact',
        'horizontal_clipped_thrust': 'replaced by shaft torque, physical friction and shaft support reaction',
        'old_success': 'development reference only; never current task-pass evidence'}
    p['assumption_policy'] = {
        'all_new_fields_pending': True,
        'mass_envelope_origin': 'AI suggested physical completion, not measured CAD or weighed parts',
        'chassis_local_inertia': '1.2kg*m2 distribution assumption within bounding radius, not uniform-box calculation',
        'control_supply_origin': 'AI completion; 48V/60Ah/80W and Crr0.05 from separate user energy reference',
        'source_geometry_preserved': ['wheel radii','axle offsets','fixed driven nose','loaded mass','stair dimensions'],
        'changes_require_user_approval': True}
    p.pop('passive_damping_implementation')
    p['passive_joint_damping'] = {}  # Explicitly none in this fixed-carrier topology.
    p['provenance_notes']['geometry'] = 'User wheel radii and relative axle geometry retained; body COM/envelopes/inertias explicitly proposed; no rocker in this package'
    p['contact_per_physical_wheel']['source'] = 'B development-fixture k/c reused as pending assumptions; B acceptance does not prove whole-car applicability; mu from user source assumption'
    p['preparation_generator_version'] = VERSION
    p['physical_digest'] = digest(p)
    return {'design': d, 'physical': p, 'binding': binding_preview(d),
            'reuse_static_reference': prior_static}


def verification_contract():
    c = deepcopy(historical_contract())
    c['schema'] = VERSION+'-verification'
    c['scope'] = 'one user fixed-nose completion draft; seven vehicle cases plus two extension probes'
    c['budget_proposal'].update(scene_attempts_per_complete_round=18, maximum_scene_attempts=54,
        new_local_execution_checks_limit=24, sysml_parse_limit=3,
        sysml_parse_scope='at most once per source repair round for this one vehicle; maximum3 rounds',
        note='NEW PROPOSAL ONLY: old84 is not inherited;2 probes+7 cases, each base/refined; reserve complete affected set')
    c['physical']['front_first_rule']['scope'] = 'original rule for isolated descent; serial descent uses dynamic upper-platform reference below'
    c['physical']['front_first_rule']['edge_crossing_order'].pop('rocker_bogie')
    c['physical']['front_first_rule']['actual_drop_order'].pop('rocker_bogie')
    c['physical'].pop('passive_joint_relative_angle_limit_deg')
    c['physical']['energy_balance_terms'] = 'delta(K+Ug+Uelastic)+integral(contact_losses+rolling_loss)-integral(shaft_power); no passive-joint damper in this topology'
    c['physical']['serial_mission_rule'] = {
        'required_risers_up':20, 'required_risers_down':20, 'platform_m':2,
        'no_reset': ['time','position','attitude','wheel_speed','controller_state','battery_energy','contact_energy'],
        'descent_event_reference': 'for each main axle, mean centre height over last qualifying0.1s upper-horizontal support window before losing that support near first descent edge; detect first10mm drop after that window, even before axle crosses x9.6',
        'support_reference_window': 'after axle enters plateau x>=7.6 and before first descent: continuously Fn_upper>5N for0.1s, centre-height range<=0.005m and abs(vz)<=0.02m/s; take latest qualifying window before upper support loss; if absent mark event evidence INSUFFICIENT, never invent a baseline',
        'edge_crossing_order': ['nose','front','rear'], 'actual_drop_order': ['front','rear'],
        'heading': '+x and abs(pitch)<=60deg; no reverse substitute',
        'upper_support': 'use actual upper feature support history; each main axle must have a qualifying reference window, lose upper support and acquire lower features; observe nose history but do not assume nose airborne; if nose un-supported only trace crossing/vertical motion/later contact, never fabricate liftoff',
        'event_evidence': 'record every axle crossing every up/down riser and its associated contact/vertical motion; x crossings alone insufficient',
        'finish': 'all wheel disks and nonwheel envelopes beyond15.2+0.5m, then continue cruise; no platform reset',
        'stop_time_s':100, 'comparison': 'same absolute2ms event comparison and other named numeric tolerances'}
    c['physical']['initialization_reference'] = '10mm nominal release; free settling0-3s; ramp3-5s; no initial pose or vehicle velocity imposed after t0'
    c['physical']['exclusions'] = 'No artificial50*v translational loss or300*omega pitch damper; no regen; rolling loss explicitly integrated'
    c['budget_proposal']['preparation_accounting'] = 'Pre-confirmation mapping/proposal checks are recorded in separate closed preparation ledgers; the proposed24 checks are NEW execution-preflight attempts, not a reset or replacement of that history'
    c['case_order'] = ['shared_supply_probe','rolling_resistance_probe','fixed_nose_flat_brake',
        'fixed_nose_three_up','fixed_nose_three_down','fixed_nose_twenty_up',
        'fixed_nose_twenty_down','fixed_nose_flat_energy','fixed_nose_full_mission']
    return c


def validate(bundle):
    """Current single draft preflight, not a general design validator or runtime pass."""
    d, p = bundle['design'], bundle['physical']
    require_valid(d, load_library())
    expected = proposal()
    if bundle != expected:
        raise ValueError('CURRENT_COMPLETION_DRAFT_ONLY: changes require explicit revision')
    if bundle['binding'] != binding_preview(d):
        raise ValueError('STALE binding')
    mass = bundle['binding']['mass_check']
    if not math.isclose(mass['total_kg'],85,abs_tol=1e-9) or mass['payload_kg'] != 20:
        raise ValueError('Wrong mass accounting')
    mapping = baseline_mapping()
    wheels = {w['id']:w for w in d['wheels']}
    # Compare axle DIFFERENCES: choosing a new body COM must not alter wheel geometry.
    ref = wheels['front']['pose']
    src_ref = next(w for w in mapping['wheels'] if w['id']=='front')
    for source in mapping['wheels']:
        wheel = wheels[source['id']]
        if wheel['radius']['value'] != source['radius_m'] or not wheel['driven'] or wheel['count']!=2:
            raise ValueError('Source wheel shape/drive/count changed')
        for k,axis in enumerate(('x','z')):
            delta = wheel['pose'][axis]['value']-ref[axis]['value']
            expected = source['local_axle_xz_m'][k]-src_ref['local_axle_xz_m'][k]
            if abs(delta-expected)>1e-12: raise ValueError('Source axle geometry changed')
    # Necessary planar inertia realizability bound, NOT proof of actual mass distribution.
    items = {i['id']:i for i in [*d['bodies'],*d['drives'],d['power_supply']]}
    inertia_bounds = []
    for cid, shapes in p['envelopes_local_xz'].items():
        radii = []
        for shape in shapes:
            points = shape['vertices'] if shape['type']=='polygon' else [shape['start'],shape['end']]
            radii.extend(math.hypot(*v)+shape.get('radius',0) for v in points)
        item = items[cid]
        upper = item['mass']['value']*max(radii)**2
        actual = item['inertia']['value']
        if not 0 < actual <= upper:
            raise ValueError('Local inertia impossible inside envelope bound: '+cid)
        inertia_bounds.append({'id':cid,'J_per_physical_instance':actual,'necessary_upper_bound':upper})
    if len(p['cases'])!=7: raise ValueError('Seven cases required')
    for case in p['cases']:
        terrain_segments(case['terrain_points'])
    full = p['cases'][-1]
    verts = [b[1]-a[1] for a,b in zip(full['terrain_points'],full['terrain_points'][1:]) if a[0]==b[0]]
    if sum(v>0 for v in verts)!=20 or sum(v<0 for v in verts)!=20:
        raise ValueError('Serial flight incomplete')
    # Projected free-motion estimate only; not a lower bound on runtime or stair feasibility.
    latest_x_needed = full['exit_plane_x_m']+full['exit_margin_m']+.357
    estimate = 5+latest_x_needed/.4
    return {'status':'PASS_CURRENT_DRAFT_STATIC_ONLY_PENDING_USER_CONFIRMATION',
            'design_digest':d['design_digest'], 'physical_digest':p['physical_digest'],
            'verification_digest':digest(verification_contract()),
            'source_mapping_digest':p['source_mapping_digest'],
            'mass_check':mass, 'source_geometry':'USER_WHEEL_RADII_AND_RELATIVE_AXLE_GEOMETRY_PRESERVED',
            'original_inertia_kg_m2':20,'original_nominal_com_z_m':.127,
            'inertia_bound_checks':inertia_bounds,
            'static_envelope_and_ratings':deepcopy(bundle['reuse_static_reference']),
            'serial_free_motion_estimate_s':estimate,'serial_stop_time_s':full['stop_time_s'],
            'not_proven':['dynamic equilibrium','real mass distribution','collision through stairs',
                          'stair climbing','serial continuity','energy accuracy','physical hardware performance']}
