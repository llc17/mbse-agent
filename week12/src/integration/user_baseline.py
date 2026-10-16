"""Read-only source evidence and componentization intent, never an approved design.

This intentionally does not import the superseded two-vehicle proposal builder.
Only arithmetic numeric parameter expressions are interpreted, never Modelica code.
"""
from __future__ import annotations

import ast
import hashlib
import math
from pathlib import Path
import re

from week12.src.component_physics.contract import ROOT
from week12.src.integration.preview import require_b_source

VERSION = 'v11-c-user-baseline-mapping-1'
SOURCE = ROOT / 'models/user-4w2n/source-v1/运动/6lun_zhenggeliucheng.mo'
ENERGY_SOURCE = ROOT / 'models/user-4w2n/source-v1/能量/power_limit.mo'
REQUIRED = ('m', 'g', 'I_pitch', 'R_main', 'R_nose', 'T_main_max',
            'T_nose_max', 'wheelbase', 'L_nose_ext', 'h_nose', 'a', 'b',
            'L_nose_CoM', 'stepH', 'stepD', 'n_steps', 'x_start_up',
            'x_end_up', 'len_flat', 'x_start_down', 'x_end_down', 'H_peak',
            'k_ground', 'c_ground', 'mu_s')


def numeric_parameters(source: str) -> dict:
    """Restricted extractor for the declared parameter subset; reject other syntax."""
    rows = {}

    def number(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.Name) and node.id in rows:
            return rows[node.id]['value']
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = number(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            a, b = number(node.left), number(node.right)
            if isinstance(node.op, ast.Add): return a + b
            if isinstance(node.op, ast.Sub): return a - b
            if isinstance(node.op, ast.Mult): return a * b
            if isinstance(node.op, ast.Div): return a / b
        raise ValueError('Unsupported parameter expression')

    pattern = re.compile(r'^\s*parameter\s+(Real|Integer)\s+(\w+)\s*=\s*([^;]+);')
    for line_no, line in enumerate(source.splitlines(), 1):
        match = pattern.match(line)
        if not match or match[2] not in REQUIRED:
            continue
        kind, name, expression = match.groups()
        if name in rows:
            raise ValueError('Duplicate parameter: ' + name)
        value = number(ast.parse(expression, mode='eval').body)
        if not math.isfinite(value) or (kind == 'Integer' and int(value) != value):
            raise ValueError('Nonfinite or invalid integer parameter: ' + name)
        rows[name] = {'value': value, 'line': line_no, 'expression': expression.strip(),
                      'origin': 'user_supplied_source_not_C_approval'}
    missing = sorted(set(REQUIRED) - rows.keys())
    if missing:
        raise ValueError('Missing required source parameters: ' + ','.join(missing))
    return rows


def mission_terrain(*, steps, rise, tread, entry, platform, start=-2., tail=3.):
    """One common directed polyline: up -> full last tread -> platform -> down.

    This is a proposed unification of the original per-wheel height functions.
    It is NOT a claim that those three original height functions were identical.
    """
    if type(steps) is not int or steps < 1:
        raise ValueError('Positive integer step count required')
    values = (rise, tread, entry, platform, start, tail)
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Nonfinite terrain')
    if min(rise, tread, platform, tail) <= 0 or start >= entry:
        raise ValueError('Invalid terrain dimensions')
    points = [[start, 0.]]
    for i in range(steps):
        x = entry + i*tread
        points.extend([[x, i*rise], [x, (i+1)*rise]])
    up_end = entry + steps*tread
    down_start = up_end + platform
    points.append([up_end, steps*rise])
    for i in range(steps):
        x = down_start + i*tread
        points.extend([[x, (steps-i)*rise], [x, (steps-i-1)*rise]])
    down_end = down_start + steps*tread
    points.extend([[down_end, 0.], [down_end+tail, 0.]])
    return {'points_xz_m': points, 'up_end_x_m': up_end,
            'down_start_x_m': down_start, 'down_end_x_m': down_end,
            'platform_length_m': platform,
            'up_risers': steps, 'down_risers': steps,
            'interpretation': 'full tread extent retained at each flight end',
            'status': 'PROPOSED_COMMON_GEOMETRY_NOT_SIMULATED'}


def baseline_mapping(source_path: Path = SOURCE):
    raw = source_path.read_bytes()
    rows = numeric_parameters(raw.decode('utf-8-sig'))
    p = {name: row['value'] for name, row in rows.items()}
    if not math.isclose(p['wheelbase'], p['a']+p['b'], abs_tol=1e-12):
        raise ValueError('Source wheelbase inconsistent with axle offsets')
    if p['m'] != 85 or p['stepH'] != .15 or p['stepD'] != .28 or p['n_steps'] != 20:
        raise ValueError('Source differs from user-selected mission facts')
    wheels = []
    for name, x, z, radius, torque in (
        ('rear', -p['b'], 0., p['R_main'], p['T_main_max']),
        ('front', p['a'], 0., p['R_main'], p['T_main_max']),
        ('nose', p['L_nose_CoM'], p['h_nose']-p['R_main'], p['R_nose'], p['T_nose_max'])):
        wheels.append({'id': name, 'physical_count': 2, 'driven': True,
                       'carrier': 'chassis', 'local_axle_xz_m': [x, z],
                       'radius_m': radius, 'B_component': 'V11PhysicalB1.WheelPair',
                       'bearing': 'absorb_A_wheel_revolute_once',
                       'original_contact_T_over_R_N': torque/radius,
                       'proposed_pair_torque_cap_Nm': torque,
                       'proposed_single_wheel_torque_cap_Nm': torque/2,
                       'torque_interpretation_status': 'PENDING_CONFIRMATION',
                       'required_missing': ['wheel_mass', 'wheel_spin_inertia', 'continuous_power']} )
    unresolved = [
        {'id': 'mass_inertia', 'required': '分件质量、位置、轮转动惯量；85kg内含20kg，不重复计质量',
         'disposition': '保留原集中CoM/I=20为输入证据；不自动改为旧提案CoM=.248/I=4.802，也不声称轮自由转动后动力学等价'},
        {'id': 'body_envelope', 'required': '车体/设备/电池/支架碰撞包络及惯量可实现性',
         'disposition': '原件缺失；待补充假设，不用轮盘无穿透冒充整车不碰阶'},
        {'id': 'drive_control', 'required': '扭矩按轮对解释、连续功率、效率、速度反馈/下楼制动',
         'disposition': '原件水平力控制须映射成轴扭矩；不照抄摩擦截断力作为驱动'},
        {'id': 'contact_losses', 'required': 'B接触k/c、滚阻、外部阻尼是否保留及对应耗散',
         'disposition': '原件4e4/3e3与B夹具参数均不自动当成C已确认值；50*v和300*omega不是已证明的器件损失'},
        {'id': 'shared_supply', 'required': '单一母线容量/电流/功率/附件耗能/非回生制动',
         'disposition': '能量原件48V/60Ah/80W是参考；旧新提案45A/.8效率未批准'},
        {'id': 'execution_contract', 'required': '初态、时窗、数值/物理容差、有限批次、批准绑定',
         'disposition': '旧两结构84次预算未批准且范围已变化，不继承；新增完整串联工况须计入新预算'}]
    return {'version': VERSION, 'status': 'SOURCE_MAPPED_PHYSICAL_COMPLETION_PENDING',
            'executable': False, 'human_confirmed': False,
            'source': {'path': str(source_path.relative_to(ROOT.parent)),
                       'sha256': hashlib.sha256(raw).hexdigest(), 'parameters': rows},
            'energy_reference': {'path': str(ENERGY_SOURCE.relative_to(ROOT.parent)),
                                 'sha256': hashlib.sha256(ENERGY_SOURCE.read_bytes()).hexdigest(),
                                 'status': 'REFERENCE_ONLY_NOT_INTEGRATED_OR_APPROVED'},
            'accepted_B_sha256': require_b_source(),
            'user_confirmed_facts': {'loaded_mass_kg': 85, 'payload_included_kg': 20,
                                     'vehicle_without_payload_kg': 65, 'selected_source': 'full_mission'},
            'source_inertial_reference': {'mass_kg': p['m'], 'pitch_inertia_kg_m2': p['I_pitch'],
                                          'nominal_com_xz_m': [0., p['R_main']],
                                          'initial_com_z_m': p['R_main']+.01,
                                          'status': 'SOURCE_LUMPED_MODEL_NOT_PARTS_MEASUREMENT'},
            'wheels': wheels,
            'root': {'free_dof': ['x','z','pitch'], 'class': 'Modelica.Mechanics.MultiBody.Joints.Planar'},
            'full_mission': mission_terrain(steps=int(p['n_steps']), rise=p['stepH'], tread=p['stepD'],
                                            entry=p['x_start_up'], platform=p['len_flat']),
            'original_model_corrections': [
                '三轮组旧高度函数上/下行阶面切换并不相同；改为同一离散轮廓并保留事件',
                '原v用于水平动力学但der(x)又混合psi/vz；统一世界坐标速度与力',
                '原俯仰仅用固定水平臂乘Fz；改由实际接触点力/力矩和驱动反作用求解',
                '新增轮转动和滑移；不强制复现旧轨迹或继承旧成功结论'],
            'unresolved': unresolved,
            'assembly_scope': 'user_fixed_nose_first; rocker_reuse_comparison_later',
            'blocked_outputs': ['approved_A_design', 'executable_Modelica', 'executable_SysML', 'simulation']}
