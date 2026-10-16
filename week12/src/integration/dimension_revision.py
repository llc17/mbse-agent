"""Exact AI dimension revision: source-only draft, static review and lowering.

No prepare/freeze, approval receipt, execution, simulator, parser or online call.
The approved C2 bundle is immutable provenance, not authority for this revision.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from types import FunctionType

from ..composition.schema import digest, load_library, require_valid, seal_design
from ..component_physics.contract import terrain_segments, world_port
from .preview import binding_preview, pose
from . import model_generator as original_generator
from . import vehicle_amendment as vehicle

ROOT = Path(__file__).resolve().parents[2]
VERSION = 'v11-c-ai-dimension-revision-1'
SOURCE = 'AI_dimension_revision_pending_concrete_confirmation'
SEED_DIRECTORY = ROOT / 'results/s3-c-vehicle-c2-execution-20261005-01'
SEED_HASHES = {
    'proposal.json': 'de89673a11ab4ba5d0227e21f3b4195e1b1ef38b46f6d06ce0e10f3ef1037515',
    'verification-contract.json': 'a6f1ce8ee4335da51df77b3c10f8d0678d7b67e9c34da0d170cf99f1b2eee8bd',
}
CASE_ORDER = ('fixed_nose_flat_brake', 'fixed_nose_three_up')
VARIANTS = ('base', 'refined')
PITCH_LENGTH = math.sqrt(.28**2 + .15**2)
MAIN_WHEELBASE = 2 * PITCH_LENGTH
NOSE_OFFSET = .330
RADIUS = .16
CONTROLLER_X = 85*.24 - (16*MAIN_WHEELBASE + 1.98) - 20*.2 - 20*.12 - 18*.1
BUDGET = {
    'scene_attempts_per_complete_round': 4, 'maximum_rounds': 3,
    'maximum_scene_attempts': 12, 'build_timeout_s': 180,
    'simulation_timeout_s': 180, 'postprocess_timeout_s': 60,
    'execution_total_wall_s': 6000, 'product_online_calls': 0,
    'new_local_execution_checks_limit': 24, 'new_local_timeout_s': 10,
    'sysml_parse_limit': 3, 'sysml_parse_timeout_s': 60,
    'identity_probe_limit': 0, 'complete_round_reserve_s': 2000,
    'local_checks_per_complete_round': 8,
    'note': 'NEW pending proposal: 8 local +1 new SysML parse +4 scenes per full round; at most3 rounds; no inherited balances',
    'repair_boundary': 'first failure pauses dependent execution; implementation-only repair within remaining complete rounds; physical/task/numerical hard failure stops without tuning',
    'success_closes_unused_budget': True,
}


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed():
    records = []
    for filename, expected in SEED_HASHES.items():
        path = SEED_DIRECTORY / filename
        if _sha(path) != expected:
            raise ValueError('IMMUTABLE_APPROVED_C2_INPUT_DRIFT: ' + filename)
        records.append(json.loads(path.read_text(encoding='utf-8')))
    bundle, contract = records
    if (bundle['physical']['wheel_pair_lowering']['stiffness_N_m'] != 100000 or
            bundle['physical']['wheel_pair_lowering']['damping_Ns_m'] != 200):
        raise ValueError('ORIGINAL_C2_C200_REQUIRED_NOT_DAMPING_DIAGNOSTIC')
    return bundle, contract


def _set_pose(record, values):
    for axis, value in zip(('x', 'z', 'pitch'), values):
        record[axis]['value'] = value
        record[axis]['source'] = SOURCE


def _mark_sources(value):
    """Mark component fields while preserving actual task facts elsewhere."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'source':
                value[key] = SOURCE
            else:
                _mark_sources(child)
    elif isinstance(value, list):
        for child in value:
            _mark_sources(child)


def _differences(before, after, prefix=''):
    """Exact changed JSON leaves, including provenance; not a tuning allowlist."""
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            path = prefix + '/' + key
            if key not in before or key not in after:
                result.append({'path': path, 'before': deepcopy(before.get(key)),
                               'after': deepcopy(after.get(key))})
            else:
                result.extend(_differences(before[key], after[key], path))
        return result
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [row for i, (a, b) in enumerate(zip(before, after))
                for row in _differences(a, b, prefix + '/' + str(i))]
    return [] if before == after else [{'path': prefix, 'before': deepcopy(before), 'after': deepcopy(after)}]


def _geometry(design, physical):
    """Independent finite algebra only; reuse the documented A/B conventions."""
    all_items = [*design['bodies'], *design['wheels'], *design['drives'], design['power_supply']]
    items = {row['id']: row for row in [*all_items, *design['joints']]}
    total = sum(i['count']*i['mass']['value'] for i in all_items)
    com = [sum(i['count']*i['mass']['value']*pose(i['pose'])[k] for i in all_items)/total for k in (0, 1)]
    inertia = sum(i['count']*(i['inertia']['value'] + i['mass']['value']*
                  sum((pose(i['pose'])[k]-com[k])**2 for k in (0, 1))) for i in all_items)
    wheel_drive_mass = sum(i['count']*i['mass']['value'] for i in [*design['wheels'], *design['drives']])
    if not (math.isclose(total, 85, abs_tol=1e-10) and
            all(math.isclose(a, b, abs_tol=1e-12) for a, b in zip(com, (.24, .26))) and
            math.isclose(wheel_drive_mass, 26, abs_tol=1e-12)):
        raise ValueError('EXACT_MASS_COM_AND_FIXED_AXLE_MASS_REQUIRED')
    if next(i for i in design['bodies'] if i['id'] == 'payload')['mass']['value'] != 20:
        raise ValueError('EXACT_PAYLOAD_REQUIRED')

    closure = []
    for connection in design['connections']:
        a, b = (endpoint.split('.') for endpoint in (connection['from'], connection['to']))
        ia, ib = items[a[0]], items[b[0]]
        pa, pb = ia['ports'][a[1]], ib['ports'][b[1]]
        if pa['type'] != 'frame2d':
            continue
        wa = world_port(pose(ia['pose']), pose(pa['pose']))
        wb = world_port(pose(ib['pose']), pose(pb['pose']))
        error = max(abs(x-y) for x, y in zip(wa, wb))
        if error > 1e-12:
            raise ValueError('NONCLOSED_FRAME_CONNECTION: ' + connection['id'])
        closure.append({'id': connection['id'], 'maximum_error_m_or_rad': error})
    gaps, bounds = [], []
    wheels = design['wheels']
    for index, wheel in enumerate(wheels):
        x, z, _ = pose(wheel['pose']); radius = wheel['radius']['value']
        if (wheel['count'] != 2 or not wheel['driven'] or radius != RADIUS or
                abs(z-radius) > 1e-12 or not math.isclose(wheel['inertia']['value'],
                .5*wheel['mass']['value']*radius**2, abs_tol=1e-14)):
            raise ValueError('EXACT_DRIVEN_WHEEL_PAIR_REQUIRED')
        bounds.extend([(x-radius, z-radius), (x+radius, z+radius)])
        for other in wheels[index+1:]:
            ox, oz, _ = pose(other['pose'])
            gap = math.hypot(x-ox, z-oz) - radius - other['radius']['value']
            if gap < .010 - 1e-12:
                raise ValueError('WHEEL_DISK_GAP_BELOW_TEN_MM')
            gaps.append({'pair': [wheel['id'], other['id']], 'clear_gap_m': gap})
    envelope_ids = {i['id'] for i in [*design['bodies'], *design['drives'], design['power_supply']]}
    if set(physical['envelopes_local_xz']) != envelope_ids:
        raise ValueError('COMPLETE_NONWHEEL_ENVELOPES_REQUIRED')
    clearance = math.inf
    inertia_bounds = []
    for cid, shapes in physical['envelopes_local_xz'].items():
        max_radius = 0
        for shape in shapes:
            if shape['type'] not in ('polygon', 'capsule'):
                raise ValueError('REGISTERED_ENVELOPE_SHAPE_REQUIRED')
            points = shape['vertices'] if shape['type'] == 'polygon' else [shape['start'], shape['end']]
            radius = shape.get('radius', 0)
            for point in points:
                x, z, _ = world_port(pose(items[cid]['pose']), (*point, 0))
                clearance = min(clearance, z-radius)
                bounds.extend([(x-radius, z-radius), (x+radius, z+radius)])
                max_radius = max(max_radius, math.hypot(*point)+radius)
        upper = items[cid]['mass']['value']*max_radius**2
        actual = items[cid]['inertia']['value']
        if not 0 < actual <= upper:
            raise ValueError('LOCAL_INERTIA_EXCEEDS_NECESSARY_ENVELOPE_BOUND: ' + cid)
        inertia_bounds.append({'id': cid, 'J_per_instance_kg_m2': actual, 'necessary_upper_bound_kg_m2': upper})
    if clearance < 0:
        raise ValueError('NONWHEEL_FLAT_TERRAIN_INTERSECTION')
    power = sum(i['count']*i['power']['value']/i['efficiency']['value'] for i in design['drives']) + physical['supply']['auxiliary_W']
    if (power > physical['supply']['continuous_power_W'] or physical['supply']['continuous_power_W'] !=
            physical['supply']['voltage_V']*physical['supply']['continuous_current_A']):
        raise ValueError('SHARED_SUPPLY_RATINGS_INCOMPATIBLE')
    reference = {'status': 'STATIC_ALGEBRA_ONLY_PENDING_CONFIRMATION', 'loaded_mass_kg': total,
        'nonwheel_initial_min_clearance_m': clearance,
        'initial_dimensions_m': {'length': max(x for x, z in bounds)-min(x for x, z in bounds),
             'height': max(z for x, z in bounds)-min(z for x, z in bounds), 'width_assumption': physical['width_m']},
        'all_drive_rated_electrical_plus_aux_W': power,
        'shared_supply_power_margin_W': physical['supply']['continuous_power_W']-power,
        'limits': ['Necessary static algebra only; no trajectory or self collision proof',
                   'No equilibrium, stair success, hardware or full mission acceptance']}
    return {'mass_kg': total, 'payload_kg': 20, 'wheel_and_drive_mass_kg': wheel_drive_mass,
        'nominal_com_xz_m': com, 'inertia_about_com_kg_m2': inertia,
        'frame_connection_closure': closure, 'wheel_disk_clearances': gaps,
        'inertia_necessary_bounds': inertia_bounds, 'reference': reference}


def build_draft():
    """Return a fresh exact candidate and pending contract; read/write no receipt."""
    seed, seed_contract = _seed()
    bundle, contract = deepcopy(seed), deepcopy(seed_contract)
    design, physical = bundle['design'], bundle['physical']
    design.update(design_id='c_ai_dimension_4w2n_20261006', revision='dimension_revision_1_pending',
                  baseline_version_id='approved_c2_geometry_revised_20261006')
    design['provenance'] = {'origin': SOURCE,
        'reference': 'approved C2 seed proposal sha256=' + SEED_HASHES['proposal.json'],
        'scope': 'User structure retained; dimensions and mass layout revised by AI, pending concrete approval; no independent blind design claim'}
    coordinates = {'rear': (0., RADIUS, 0.), 'front': (MAIN_WHEELBASE, RADIUS, 0.),
        'nose': (MAIN_WHEELBASE+NOSE_OFFSET, RADIUS, 0.), 'chassis': (.2, .205, 0.),
        'payload': (.12, .4285, 0.), 'battery': (.1, .275, 0.), 'controller': (CONTROLLER_X, .32, 0.)}
    components = {i['id']: i for i in [*design['bodies'], *design['wheels'], *design['joints'],
                                      *design['drives'], design['power_supply']]}
    for group in ('bodies', 'wheels', 'joints', 'drives'):
        _mark_sources(design[group])
    _mark_sources(design['power_supply'])
    _mark_sources(design['connections'])
    for cid, position in coordinates.items():
        _set_pose(components[cid]['pose'], position)
    components['chassis']['mass']['value'] = 20
    for wheel in design['wheels']:
        wheel['radius']['value'] = RADIUS
        wheel['mass']['value'] = 2 if wheel['id'] == 'nose' else 3
        wheel['inertia']['value'] = .5*wheel['mass']['value']*RADIUS**2
        _set_pose(components[wheel['id']+'_drive']['pose'], coordinates[wheel['id']])
        _set_pose(components[wheel['id']+'_bearing']['pose'], coordinates[wheel['id']])
    for cid in ('payload', 'controller'):
        _set_pose(components[cid+'_mount']['pose'], coordinates[cid])
    # All structural ports at recomputed joint world coordinates.  Non-body
    # ports remain central; the original connection IDs/topology are retained.
    chassis = components['chassis']
    for port_name in chassis['ports']:
        target = coordinates[port_name.removesuffix('_bearing').removesuffix('_mount')]
        local = tuple(a-b for a, b in zip(target, coordinates['chassis']))
        _set_pose(chassis['ports'][port_name]['pose'], local)
    design = seal_design(design)
    require_valid(design, load_library())
    bundle['design'] = design
    bundle['binding'] = binding_preview(design)

    physical.update(schema=VERSION, status='PROPOSED_AI_DIMENSION_REVISION_NOT_AUTHORIZED',
        design_digest=design['design_digest'], preparation_generator_version=VERSION)
    # Old source mapping is history only and cannot certify the revised sizes.
    physical['source_history'] = {key: physical.pop(key) for key in
        ('source_mapping_digest', 'source_model_sha256', 'energy_reference_sha256')}
    physical['source_history'].update(seed_proposal_sha256=SEED_HASHES['proposal.json'],
        seed_contract_sha256=SEED_HASHES['verification-contract.json'],
        old_original_terms_disposition=deepcopy(physical['original_terms_disposition']),
        note='Original mappings and results are historical; no revised geometry attributed to the user source')
    physical['original_terms_disposition']['original_COM_at_main_axle'] = 'Not retained; AI dimensions and complete ledger compute nominal COM(.24,.26)'
    physical['provenance_notes']['geometry'] = SOURCE + '; all six tire radii160mm; fixed nose; no rocker'
    physical['provenance_notes']['mass_split'] = SOURCE + ';85kg including20kg payload, unweighed hardware'
    physical['assumption_policy']['source_geometry_preserved'] = ['fixed driven nose topology', 'loaded mass', 'stair dimensions and full task facts']
    physical['assumption_policy']['dimension_revision_origin'] = SOURCE
    physical['assumption_policy']['chassis_local_inertia'] = 'Retained1.2kg*m2 AI distribution assumption; necessary envelope bound only, not uniform-box calculation'
    front = [MAIN_WHEELBASE-.2, RADIUS-.205]
    nose = [MAIN_WHEELBASE+NOSE_OFFSET-.2, RADIUS-.205]
    payload = [.12-.2, .4285-.205]
    physical['envelopes_local_xz']['chassis'] = [deepcopy(seed['physical']['envelopes_local_xz']['chassis'][0]),
        {'type': 'capsule', 'start': [0., 0.], 'end': front, 'radius': .015},
        {'type': 'capsule', 'start': front, 'end': nose, 'radius': .015},
        {'type': 'capsule', 'start': [0., 0.], 'end': payload, 'radius': .015}]
    physical['housing_amendment'].update(origin=SOURCE,
        mass_inertia_pose_ratings='Original per-instance mass/inertia/ratings retained; pose follows revised nose axle',
        conditional_clearance_m=RADIUS-.05-min(.02, .15*RADIUS),
        clearance_condition='Revised R160mm axle-centred wheel; unchanged penetration rule yields20mm; conditional algebra only',
        gate_bound_condition='Historical gates do not validate revised geometry; new direct clearance evidence required')
    physical['housing_amendment'].pop('clearance_bound_from_existing_two_gates_m', None)
    physical['dimension_revision'] = {'origin': SOURCE, 'p_m': PITCH_LENGTH,
        'main_wheelbase_m': MAIN_WHEELBASE, 'nose_offset_m': NOSE_OFFSET,
        'controller_x_m': CONTROLLER_X, 'nominal_com_target_xz_m': [.24, .26],
        'penetration_rule': seed_contract['physical']['wheel_penetration_limit'],
        'derived_individual_penetration_limit_m': min(.02, .15*RADIUS),
        'rule_change': False, 'old_numeric_limits_are_not_reused': True,
        'execution_scope': list(CASE_ORDER),
        'remaining_task_definitions': 'Retained in physical.cases as facts only; not executable by this source-only API'}
    bundle['reuse_static_reference'] = _geometry(design, physical)['reference']
    vehicle._seal_physical(physical)
    contract.update(schema=VERSION+'-verification', status='PROPOSED_NOT_EXECUTION_AUTHORIZATION',
        case_order=list(CASE_ORDER), scope='one AI dimension revision; flat brake then three-up; no descent/20-step/full-mission authority',
        budget_proposal=deepcopy(BUDGET))
    contract['out_of_scope_contract_notes'] = {
        'historical_descent_rules_retained_for_threshold_identity_only': True,
        'nose_exception': 'Old front_first_rule.nose_exception describes R70mm nose initially airborne120mm. Revised R160mm nose is nominally tangent with both main pairs; this old clause is inapplicable and is not an executable descent protocol.',
        'future_descent': 'Requires explicit revised geometry event/support review and concrete confirmation before use; all seven original case definitions retained as task facts only.',
        'sysml': 'Geometry changed; old parsed bytes cannot be reused as the revised structure parse evidence.',
    }
    return bundle, contract


def validate(bundle, contract):
    """Fail closed outside the exact candidate; never claim physical PASS."""
    expected_bundle, expected_contract = build_draft()
    if bundle != expected_bundle or contract != expected_contract:
        raise ValueError('EXACT_DIMENSION_REVISION_AND_CONTRACT_ONLY')
    design, physical = bundle['design'], bundle['physical']
    require_valid(design, load_library())
    if bundle['binding'] != binding_preview(design):
        raise ValueError('RECOMPUTED_BINDING_REQUIRED')
    raw = deepcopy(physical); checksum = raw.pop('physical_digest')
    if digest(raw) != checksum or physical['design_digest'] != design['design_digest']:
        raise ValueError('REVISED_INPUT_IDENTITY_DRIFT')
    seed, old_contract = _seed()
    unchanged_physical = ('contact_per_physical_wheel', 'wheel_pair_lowering', 'rolling_resistance',
        'supply', 'controller', 'initialization', 'passive_joint_damping', 'user_facts',
        'cases', 'contact_implementation', 'width_m', 'mass_kg', 'vehicle_without_payload_kg')
    if any(physical[k] != seed['physical'][k] for k in unchanged_physical):
        raise ValueError('OUTSIDE_GEOMETRY_MASS_REVISION')
    if any(contract[k] != old_contract[k] for k in ('physical', 'numerical', 'numerical_comparison', 'probes', 'reused_probe_evidence')):
        raise ValueError('UNCHANGED_VERIFICATION_RULES_REQUIRED')
    if (physical['b_source_sha256'] != _sha(original_generator.B_SOURCE) or
            _sha(vehicle.CANDIDATE) != vehicle.C2_SHA or
            physical['contact_implementation']['source_sha256'] != vehicle.C2_SHA or
            physical['contact_implementation']['runtime_B_copy_sha256'] !=
            hashlib.sha256(vehicle.isolated_b().encode('utf-8')).hexdigest()):
        raise ValueError('ACCEPTED_C2_AND_B_BINDING_REQUIRED')
    original_topology = [(c['id'], c['from'], c['to']) for c in seed['design']['connections']]
    if [(c['id'], c['from'], c['to']) for c in design['connections']] != original_topology:
        raise ValueError('UNCHANGED_CONNECTION_TOPOLOGY_REQUIRED')
    for case in physical['cases']:
        terrain_segments(case['terrain_points'])
    report = _geometry(design, physical)
    for cid in CASE_ORDER:
        case = next(c for c in physical['cases'] if c['id'] == cid)
        rightmost_disk = max(pose(w['pose'])[0]+w['radius']['value'] for w in design['wheels'])
        if case['initial_world_translation_z_m'] != .01 or rightmost_disk >= case['first_riser_x_m']:
            raise ValueError('ALL_INITIAL_WHEELS_MUST_START_ON_FLAT_BEFORE_FIRST_RISER')
    report.update(status='STATIC_REVIEW_ONLY_NOT_PHYSICAL_PASS_NOT_AUTHORIZED',
        design_digest=design['design_digest'], physical_digest=checksum,
        verification_digest=digest(contract), source=SOURCE,
        exact_changed_bundle_leaves=_differences(seed, bundle),
        exact_changed_contract_leaves=_differences(old_contract, contract),
        derived_wheel_penetration_limit_m=min(.02, .15*RADIUS), unchanged_penetration_rule=True,
        initial_rightmost_wheel_disk_x_m=rightmost_disk, first_riser_x_m=2.,
        initial_release_clearance_m=.01,
        absolute_task_coordinate_rule='Case entrance/exit unchanged; actual revised wheel disks and body envelopes determine exit, no old-root-position offset',
        out_of_scope_contract_notes=deepcopy(contract['out_of_scope_contract_notes']),
        authorized_execution=False, new_tests_run=0, new_simulations=0, new_sysml_parses=0,
        not_proven=['dynamic equilibrium', 'actual mass distribution', 'stair trajectory and body clearance',
                    'brake task', 'three-up task', 'numerical accuracy', '20-step/descent/full mission', 'actual hardware'])
    return report


def _isolated_generator(bundle, contract, specification_digest):
    """Clone every module-local function into one private global namespace.

    FunctionType takes code and globals.  Retaining every helper in the same
    private namespace ensures nested calls use only revised identity bindings.
    No original module global (including its _validate_bundle) is modified.
    """
    namespace = dict(vars(original_generator))
    namespace.update(VERSION=VERSION, SPECIFICATION_DIGEST=specification_digest,
        DESIGN_DIGEST=bundle['design']['design_digest'], PHYSICAL_DIGEST=bundle['physical']['physical_digest'],
        VERIFICATION_DIGEST=digest(contract))
    for name, function in vars(original_generator).items():
        if isinstance(function, FunctionType) and function.__module__ == original_generator.__name__:
            clone = FunctionType(function.__code__, namespace, function.__name__,
                                 function.__defaults__, function.__closure__)
            clone.__kwdefaults__ = deepcopy(function.__kwdefaults__)
            clone.__annotations__ = deepcopy(function.__annotations__)
            namespace[name] = clone
    return namespace['generate']


def generate(bundle, contract, case_id, specification_digest):
    """Lower exactly two revised cases to strings, without running artifacts."""
    validate(bundle, contract)
    if case_id not in CASE_ORDER:
        raise ValueError('EXACT_TWO_REVISED_CASES_ONLY')
    if not isinstance(specification_digest, str) or re.fullmatch('[0-9a-f]{64}', specification_digest) is None:
        raise ValueError('CONCRETE_REVIEW_SPECIFICATION_DIGEST_REQUIRED')
    result = _isolated_generator(bundle, contract, specification_digest)(bundle, case_id)
    trace = result['trace']
    trace.update(approval_status='PENDING_NOT_EXECUTION_AUTHORIZATION', source_origin=SOURCE,
        previous_generator_version=original_generator.VERSION,
        dimension_revision=deepcopy(bundle['physical']['dimension_revision']),
        contact_binding=deepcopy(bundle['physical']['contact_implementation']),
        source_history=deepcopy(bundle['physical']['source_history']),
        sysml_geometry_changed_requires_new_parse=True,
        executable_artifacts_run=False)
    trace['frozen']['contact_source_sha256'] = vehicle.C2_SHA
    return result
