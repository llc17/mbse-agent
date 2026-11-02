"""Pure, finite-sample geometry diagnostics for the inherited stair route.

All represented wheels see one rigid stair solid.  PASS is only the absence of
the hard contradiction checked here; it is never physical acceptance.  Small
circle/boundary overlaps are observations because no tyre calibration is bound.
"""
from bisect import bisect_right
from collections.abc import Mapping, Sequence
import math
import re


SCHEMA = 'v12-contact-validity-2'
RULE_VERSION = 'sampled-full-circle-interior-20261027-v2'
SCOPE = 'SAMPLED_CIRCULAR_WHEELS_AGAINST_ONE_RIGID_2D_STAIR_SOLID'
VERIFICATION_SCOPE = 'FINITE_SAMPLED_GEOMETRY_ONLY'
CAPABILITY_LIMITATION = '能力限制：本有限采样检查未验证轮转、滑移或同轮多接触力闭合；物理能力仍为UNKNOWN。'
ENTITIES = ('n', 'f', 'r')
PARAMETERS = ('stepH', 'stepD', 'n_steps', 'x_start_up', 'x_end_up',
              'x_start_down', 'x_end_down', 'H_peak', 'R_nose', 'R_main')
REQUIRED = ('time',) + tuple(k + e for e in ENTITIES for k in ('x_', 'z_', 'Fz_'))
LIMITATIONS = [
    'PASS仅表示有限采样未发现整圆严格进入统一刚性楼梯实体；完整物理有效性仍UNKNOWN。',
    '普通轮圆重叠和轮心进入实体单列诊断；没有获批的轮胎变形或穿透百分比阈值。',
    '没有连续碰撞时刻、车架碰撞、立面反力、摩擦、电机或能量有效性证明。',
    'Fz为已报告竖向力，Fx可能为牵引力；两者不能自动解释为立面法向反力。',
    'h_i+R_i-z_i仅是旧竖向压入代理量，不能当作一般圆轮实体穿透距离。',
    CAPABILITY_LIMITATION,
]


def verification_disclosure():
    """Fixed scope of this checker, independent of model names or state fields."""
    return dict(verification_scope=VERIFICATION_SCOPE,
                capabilities={name: 'NOT_ASSESSED' for name in
                              ('wheel_rotation', 'slip', 'multi_contact_force_closure')})


def _base(source_family=None):
    limitations=list(LIMITATIONS)
    if source_family=='source-review-bound-stair-contact-v1':
        limitations[2]='已审核源码补入统一踏面/立面/棱线接触；本几何检查不证明连续碰撞、力向量、力矩、摩擦、电机或能量有效性。'
        limitations[3]='Fx/Fz为报告的合力分量；没有分解字段时不能从合力反推出各立面/棱线接触反力。'
        limitations[4]='h_i是各轮心x处共同地表高度；h_i+R_i-z_i仍非一般圆轮实体穿透距离。'
    return dict(schema=SCHEMA, rule_version=RULE_VERSION, status='UNKNOWN',
                conclusion='INSUFFICIENT_CONTACT_EVIDENCE', physical_status='UNKNOWN',
                scope=SCOPE, issues=[], limitations=limitations,
                counters={}, per_entity={}, hard_contradictions=[], observations={},
                **verification_disclosure())


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('missing/nonfinite/nonnumeric contact evidence: ' + label)
    return float(value)


def _geometry(values):
    if not isinstance(values, Mapping):
        raise ValueError('contact parameters must be a mapping')
    p = {name: _number(values.get(name), name) for name in PARAMETERS}
    count = p['n_steps']
    if not count.is_integer() or not 1 <= count <= 4096:
        raise ValueError('unsupported stair count: require integer 1..4096 (diagnostic resource scope)')
    count = int(count)
    scale = max(1., *(abs(value) for value in p.values()))
    epsilon = max(1e-12, 64 * math.ulp(scale))
    for key in ('stepH', 'stepD', 'H_peak', 'R_nose', 'R_main'):
        if p[key] <= epsilon:
            raise ValueError('nonpositive or arithmetically unresolved contact parameter: ' + key)
    if not p['x_start_up'] < p['x_end_up'] <= p['x_start_down'] < p['x_end_down']:
        raise ValueError('stair route is not ordered')
    expected = dict(x_end_up=p['x_start_up'] + count * p['stepD'],
                    x_end_down=p['x_start_down'] + count * p['stepD'],
                    H_peak=count * p['stepH'])
    if 'len_flat' in values:
        flat = _number(values['len_flat'], 'len_flat')
        if flat < 0:
            raise ValueError('negative platform length')
        expected['x_start_down'] = p['x_end_up'] + flat
    if any(not math.isfinite(v) or abs(p[k] - v) > epsilon for k, v in expected.items()):
        raise ValueError('stair derived parameters disagree with one global route')
    h, d, up, down = (p[k] for k in ('stepH', 'stepD', 'x_start_up', 'x_start_down'))
    risers = []
    for phase, start in (('up', up), ('down', down)):
        for k in range(count):
            left = k * h if phase == 'up' else (count - k) * h
            right = (k + 1) * h if phase == 'up' else (count - k - 1) * h
            risers.append(dict(kind='riser', phase=phase, step=k + 1,
                               x=start + k * d, low=min(left, right), high=max(left, right),
                               left_height=left, right_height=right))
    treads = [dict(kind='tread', phase='entry_ground', step=0,
                   left=-math.inf, right=up, height=0.)]
    for k in range(count - 1):
        treads.append(dict(kind='tread', phase='up', step=k + 1,
                           left=up + k * d, right=up + (k + 1) * d, height=(k + 1) * h))
    treads.append(dict(kind='tread', phase='platform', step=count,
                       left=up + (count - 1) * d, right=down, height=count * h))
    for k in range(count - 1):
        treads.append(dict(kind='tread', phase='down', step=k + 1,
                           left=down + k * d, right=down + (k + 1) * d,
                           height=(count - k - 1) * h))
    treads.append(dict(kind='tread', phase='exit_ground', step=count,
                       left=down + (count - 1) * d, right=math.inf, height=0.))
    edges = [dict(kind='edge', phase=r['phase'], step=r['step'], x=r['x'], z=z)
             for r in risers for z in (r['low'], r['high'])]
    return p, epsilon, risers, treads, edges


def _surface_distance(x, z, surface):
    kind = surface['kind']
    if kind == 'tread':
        px = min(surface['right'], max(surface['left'], x)); pz = surface['height']
    elif kind == 'riser':
        px = surface['x']; pz = min(surface['high'], max(surface['low'], z))
    else:
        px, pz = surface['x'], surface['z']
    return math.hypot(x - px, z - pz), px, pz


def _common_height(x, risers, positions, epsilon):
    """At a jump use its lower endpoint for strict-solid-interior tests.

    Between the jump endpoints the exact x is boundary, while below both
    adjacent treads it is interior.  Arithmetic-near x values are conservative.
    """
    index = bisect_right(positions, x)
    for candidate in (index - 1, index):
        if 0 <= candidate < len(risers) and abs(x - positions[candidate]) <= epsilon:
            r = risers[candidate]
            return r['low'], r['high'], True
    height = 0. if index == 0 else risers[index - 1]['right_height']
    return height, height, False


def _counter():
    return dict(sample_count=0, center_inside_solid_sample_count=0,
                full_circle_inside_solid_sample_count=0, full_circle_boundary_uncertain_sample_count=0,
                circle_solid_overlap_sample_count=0,
                tread_overlap_sample_count=0, riser_overlap_sample_count=0,
                edge_overlap_sample_count=0, tangent_boundary_sample_count=0,
                overlap_with_zero_vertical_force_sample_count=0,
                overlap_with_zero_reported_vector_sample_count=0,
                riser_overlap_with_zero_vertical_force_sample_count=0,
                negative_vertical_force_sample_count=0,
                legacy_height_disagreement_sample_count=0)


def diagnose_contact_rows(rows, values, *, source_family=None):
    """Return a JSON-safe diagnostic from in-memory rows/parameters, without IO.

    Rows use metres, seconds and newtons.  The mandatory fields are REQUIRED;
    optional h_n/f/r and Fx_n/f/r are reported when supplied.  A wheel centre
    inside the solid is an observation. A full circle strictly inside it is a
    severe sampled geometric anomaly: center inside and nearest boundary d>R.
    Epsilon only handles floating-point uncertainty, not solver error or tyre
    deformation; no physical compression percentage or height cutoff is used.
    """
    result = _base(source_family)
    try:
        p, epsilon, risers, treads, edges = _geometry(values)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or len(rows) < 2:
            raise ValueError('at least two ordered finite contact samples required')
        # Validate every consumed field before claiming the diagnostic completed.
        clean = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ValueError('contact row must be a mapping: ' + str(index))
            copy = {key: _number(row.get(key), str(index) + ':' + key) for key in REQUIRED}
            for key in tuple(k + e for e in ENTITIES for k in ('h_', 'Fx_')):
                if key in row:
                    copy[key] = _number(row[key], str(index) + ':' + key)
            for entity in ENTITIES:
                if 'h_' + entity in copy:
                    radius = p['R_nose' if entity == 'n' else 'R_main']
                    if not math.isfinite(copy['h_' + entity] + radius - copy['z_' + entity]):
                        raise ValueError('nonfinite derived contact evidence: legacy vertical compression')
            clean.append(copy)
        if any(b['time'] < a['time'] for a, b in zip(clean, clean[1:])) or clean[-1]['time'] <= clean[0]['time']:
            raise ValueError('contact sample time does not advance monotonically')
    except (ValueError, TypeError, OverflowError) as exc:
        result['issues'].append(str(exc))
        return result

    result['parameters'] = p
    result['geometry_convention'] = dict(
        first_up_riser_x_m=p['x_start_up'], first_down_riser_x_m=p['x_start_down'],
        risers_per_direction=int(p['n_steps']), riser_coordinates='start + k*stepD, k=0..n_steps-1',
        up_end='x_end_up=start_up+n_steps*stepD ends the final high tread',
        down_end='x_end_down=start_down+n_steps*stepD ends the final ground interval',
        boundary='exact tread/riser/edge is not strict interior; below both sides of a riser is interior',
        same_solid_for_all_entities=True, ground='solid extends below z=0 and to both horizontal infinities',
        absolute_arithmetic_epsilon_m=epsilon, epsilon_basis='max(1e-12,64*ulp(parameter_scale)); not a compliance allowance')
    result['counters'] = _counter()
    result['per_entity'] = {e: dict(radius_m=p['R_nose' if e == 'n' else 'R_main'],
                                    counters=_counter(), maximum_boundary_overlap_m=0.,
                                    maximum_center_depth_m=0., maximum_full_circle_margin_m=None,
                                    maximum_legacy_vertical_compression_m=None,
                                    evidence={}) for e in ENTITIES}
    positions = [r['x'] for r in risers]
    surfaces = treads + risers + edges
    hard_rows = set(); inside_rows = set()
    for index, row in enumerate(clean):
        for entity in ENTITIES:
            detail = result['per_entity'][entity]; counts = detail['counters']
            radius = detail['radius_m']; x, z = row['x_' + entity], row['z_' + entity]
            point_epsilon = max(epsilon, 64 * math.ulp(max(1., abs(x), abs(z))))
            lower, upper, at_riser = _common_height(x, risers, positions, point_epsilon)
            inside = z < lower - point_epsilon
            distances = [_surface_distance(x, z, s) for s in surfaces]
            nearest_index = min(range(len(surfaces)), key=lambda k: distances[k][0])
            nearest_distance, px, pz = distances[nearest_index]
            # Requiring a strictly positive distance protects exact boundary points.
            inside = inside and nearest_distance > point_epsilon
            margin = nearest_distance - radius
            full_inside = inside and margin > point_epsilon
            boundary_uncertain = inside and abs(margin) <= point_epsilon
            flags = {kind: any(s['kind'] == kind and d[0] < radius - point_epsilon
                               for s, d in zip(surfaces, distances)) for kind in ('tread', 'riser', 'edge')}
            overlap = inside or any(flags.values())
            tangent = any(abs(d[0] - radius) <= point_epsilon for d in distances)
            force_z = row['Fz_' + entity]; force_x = row.get('Fx_' + entity)
            zero_vector = force_z == 0. and force_x == 0. if force_x is not None else False
            sample = dict(row=index, time_s=row['time'], entity=entity,
                          center_x_m=x, center_z_m=z, radius_m=radius,
                          strict_center_inside_solid=inside,
                          full_circle_inside_solid=full_inside,
                          full_circle_interior_margin_m=margin,
                          absolute_arithmetic_epsilon_m=point_epsilon,
                          margin_resolution='FLOATING_POINT_BOUNDARY_UNCERTAIN' if boundary_uncertain else 'SAMPLED_GEOMETRY_ONLY',
                          center_to_boundary_distance_m=nearest_distance,
                          nearest_boundary=dict(kind=surfaces[nearest_index]['kind'],
                                                phase=surfaces[nearest_index]['phase'],
                                                step=surfaces[nearest_index]['step'], x_m=px, z_m=pz),
                          common_height_lower_m=lower, common_height_upper_m=upper,
                          at_riser_x=at_riser, reported_vertical_force_N=force_z,
                          reported_horizontal_force_N=force_x)
            increments = dict(sample_count=1, center_inside_solid_sample_count=int(inside),
                              full_circle_inside_solid_sample_count=int(full_inside),
                              full_circle_boundary_uncertain_sample_count=int(boundary_uncertain),
                              circle_solid_overlap_sample_count=int(overlap),
                              tangent_boundary_sample_count=int(tangent),
                              overlap_with_zero_vertical_force_sample_count=int(overlap and force_z == 0.),
                              overlap_with_zero_reported_vector_sample_count=int(overlap and zero_vector),
                              riser_overlap_with_zero_vertical_force_sample_count=int(flags['riser'] and force_z == 0.),
                              negative_vertical_force_sample_count=int(force_z < 0.),
                              **{kind + '_overlap_sample_count': int(flag) for kind, flag in flags.items()})
            height_key = 'h_' + entity
            if height_key in row:
                proxy = max(0., row[height_key] + radius - z)
                old = detail['maximum_legacy_vertical_compression_m']
                detail['maximum_legacy_vertical_compression_m'] = proxy if old is None else max(old, proxy)
                mismatch = not lower - point_epsilon <= row[height_key] <= upper + point_epsilon
                increments['legacy_height_disagreement_sample_count'] = int(mismatch)
                if mismatch:
                    detail['evidence'].setdefault('first_legacy_height_disagreement', dict(sample, legacy_height_m=row[height_key]))
            for key, value in increments.items():
                counts[key] += value; result['counters'][key] += value
            boundary_overlap = max(0., radius - nearest_distance)
            if boundary_overlap > detail['maximum_boundary_overlap_m']:
                detail['maximum_boundary_overlap_m'] = boundary_overlap
                detail['evidence']['maximum_boundary_overlap'] = dict(sample, radial_boundary_overlap_m=boundary_overlap)
            if inside:
                inside_rows.add(index)
                detail['evidence'].setdefault('first_center_inside_solid', sample)
                if nearest_distance > detail['maximum_center_depth_m']:
                    detail['maximum_center_depth_m'] = nearest_distance
                    detail['evidence']['maximum_center_depth'] = sample
            if full_inside:
                hard_rows.add(index)
                if len(result['hard_contradictions']) < 20:
                    result['hard_contradictions'].append(sample)
                if detail['maximum_full_circle_margin_m'] is None or margin > detail['maximum_full_circle_margin_m']:
                    detail['maximum_full_circle_margin_m'] = margin
                    detail['evidence']['maximum_full_circle_inside_solid'] = sample
            if boundary_uncertain:
                detail['evidence'].setdefault('first_full_circle_boundary_uncertain', sample)
            for kind, flag in flags.items():
                if flag:
                    same_kind = [k for k, s in enumerate(surfaces) if s['kind'] == kind]
                    nearest_kind = min(same_kind, key=lambda k: distances[k][0])
                    kd, kx, kz = distances[nearest_kind]
                    surface = surfaces[nearest_kind]
                    detail['evidence'].setdefault('first_' + kind + '_overlap', dict(
                        sample, overlap_boundary=dict(kind=kind, phase=surface['phase'],
                                                      step=surface['step'], x_m=kx, z_m=kz,
                                                      distance_m=kd, radial_overlap_m=radius - kd)))
            if overlap and force_z == 0.:
                detail['evidence'].setdefault('first_overlap_with_zero_vertical_force', sample)
            if overlap and zero_vector:
                detail['evidence'].setdefault('first_overlap_with_zero_reported_vector', sample)
    result['counters']['row_count'] = len(clean)
    result['counters']['rows_with_center_inside_solid'] = len(inside_rows)
    result['counters']['rows_with_full_circle_inside_solid'] = len(hard_rows)
    result['observations'] = dict(
        circle_geometry='radial_boundary_overlap=max(0,R-distance_to_nearest_boundary); not vertical h+R-z',
        center_depth='positive shortest center-to-boundary distance, reported only for strict interior',
        severe_anomaly='strict center interior and d-R>arithmetic_epsilon; minimum over finite treads, risers and corners, same rule for n/f/r',
        numerical_uncertainty='epsilon is floating-point only, not integration error; sampled geometry is not certified continuous physical failure',
        force_scope='zero reported Fz/Fx does not identify a dedicated riser-normal reaction',
        soft_overlap_policy='report only; no uncalibrated percentage/height/diameter threshold',
        source_height_policy='legacy h disagreement is observed, not a separate hard-failure trigger',
        evidence_cap_per_list=20, continuous_collision_status='NOT_VERIFIED')
    if hard_rows:
        result.update(status='FAIL', conclusion='SAMPLED_FULL_CIRCLE_INSIDE_SOLID')
        result['issues'].append('有限采样发现整圆严格处于统一楼梯实体内部（轮心在内且最近边界距离d>R）；这是模型接触几何异常，轨迹完成也不能证明车辆能力，physical_status仍UNKNOWN。')
    else:
        result.update(status='PASS', conclusion='NO_HARD_CONTRADICTION_OBSERVED')
    return result


def diagnose_contact_source(source, values):
    """Recognize only the explicit inherited inconsistent height-formula family.

    This is a narrow source observation, not a Modelica interpreter or acceptance
    gate for arbitrary code.  Unknown formulations remain UNKNOWN.
    """
    result = _base(); result['scope'] = 'KNOWN_INHERITED_HEIGHT_FORMULA_SOURCE_DIAGNOSTIC'
    try:
        p, epsilon, _, _, _ = _geometry(values)
        if not isinstance(source, str) or not source.strip():
            raise ValueError('missing Modelica source')
        clean = re.sub(r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\r\n]*', ' ', source)
        expected = {
            'n': 'ifx<x_start_upthen0elseifx<x_end_upthen(floor((x-x_start_up)/stepD)+1)*stepHelseifx<x_start_downthenH_peakelseifx<x_end_downthenH_peak-(floor((x-x_start_down)/stepD)+1)*stepHelse0',
            'f': 'ifx<x_start_upthen0elseifx<x_end_upthenfloor((x-x_start_up)/stepD+1e-3)*stepHelseifx<x_start_downthenH_peakelseifx<x_end_downthenmax(0,H_peak-floor((x-x_start_down)/stepD+1e-3)*stepH)else0',
        }
        expected['r'] = expected['f']
        for entity in ENTITIES:
            if len(re.findall(r'\bh_' + entity + r'\s*=', clean)) != 1:
                raise ValueError('height assignment not unique: ' + entity)
            matches = re.findall(r'\bh_' + entity + r'\s*=\s*noEvent\s*\(([^;]+)\)\s*;', clean)
            if len(matches) != 1:
                raise ValueError('height formulation not uniquely recognized: ' + entity)
            compact = re.sub(r'\s+', '', matches[0]).replace('x_' + entity, 'x')
            if compact != expected[entity]:
                raise ValueError('height formulation outside known inherited source scope: ' + entity)
        result.update(status='FAIL', conclusion='INCONSISTENT_TERRAIN_FORMULAS', parameters=p,
                      same_global_x_evidence=[
                          dict(phase='up', x_m=p['x_start_up'] + p['stepD'] / 2,
                               nose_height_m=p['stepH'], front_height_m=0., rear_height_m=0.),
                          dict(phase='down', x_m=p['x_start_down'] + p['stepD'] / 2,
                               nose_height_m=p['H_peak'] - p['stepH'],
                               front_height_m=p['H_peak'], rear_height_m=p['H_peak'])],
                      absolute_arithmetic_epsilon_m=epsilon)
        result['issues'].append('已识别旧家族鼻/主轮高度公式在同一全局x处相差一级；源码缺陷不单独替代轨迹硬矛盾证据。')
    except (ValueError, TypeError, OverflowError) as exc:
        result['issues'].append(str(exc))
    return result
