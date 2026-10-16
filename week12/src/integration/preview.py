"""Deterministic, non-executable A-to-B binding plan.

Reuse A validation without changing its frozen registry. This module does not
emit Modelica, invoke a compiler, create confirmation, or simulate a car.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path

from week12.src.composition.compiler import compile_design
from week12.src.composition.schema import digest
from week12.src.component_physics.contract import ROOT, mount_offset, world_port

VERSION = 'v11-c-binding-preview-1'
B_SOURCE = ROOT / 'models/components/V11PhysicalB1/package.mo'
B_ACCEPTANCE = ROOT / 'results/s3-b-auto-repair-20260923-02/final-result.json'
B_MANIFEST = ROOT / 'results/s3-b-auto-repair-20260923-02/round-01/source-manifest.json'


def pose(value):
    return tuple(value[key]['value'] for key in ('x', 'z', 'pitch'))


def transform(parent, child):
    x, z, pitch = mount_offset(parent, child)
    return {'class': 'Modelica.Mechanics.MultiBody.Parts.FixedRotation',
            'r_m': [x, 0, z], 'n': [0, -1, 0], 'angle_deg': math.degrees(pitch)}


def component_mass(item):
    return {'m_kg': item['count'] * item['mass']['value'],
            'J_kg_m2': item['count'] * item['inertia']['value'],
            'count_applied_once': item['count']}


def require_b_source():
    import json
    accepted = json.loads(B_ACCEPTANCE.read_text(encoding='utf-8'))
    if accepted['status'] != 'PASS_S3_B_FROZEN_COMPONENT_FIXTURES':
        raise ValueError('B_ACCEPTANCE_REQUIRED')
    actual = hashlib.sha256(B_SOURCE.read_bytes()).hexdigest()
    rows = json.loads(B_MANIFEST.read_text(encoding='utf-8'))
    candidates = []
    for row in rows:
        # The manifest includes thousands of library files. Only resolve the
        # matching package path; Windows resolve() otherwise stats every file.
        if not row['path'].replace('\\', '/').endswith('/V11PhysicalB1/package.mo'):
            continue
        p = Path(row['path'])
        if not p.is_absolute():
            p = ROOT.parent / p
        if p.resolve() == B_SOURCE.resolve():
            candidates.append(row['sha256'])
    if candidates != [actual]:
        raise ValueError('B_SOURCE_DRIFT')
    return actual


def binding_preview(design):
    compiled = compile_design(design, purpose='structure_preview')
    ir = compiled['artifacts']['structure.ir.json']
    components = {c['id']: c for c in ir['components']}
    peers = {}
    for c in ir['connections']:
        peers.setdefault(c['from'], []).append(c['to'])
        peers.setdefault(c['to'], []).append(c['from'])
    chassis = next(c for c in design['bodies'] if c['role'] == 'chassis')
    connections = []
    bearings = {}
    for joint in sorted(design['joints'], key=lambda c: c['id']):
        ends = [peers[joint['id']+'.'+side][0] for side in ('a', 'b')]
        nodes = [e.split('.')[0] for e in ends]
        if joint['kind'] == 'wheel_revolute':
            wid = next(n for n in nodes if components[n]['kind'] == 'tire_pair')
            carrier_id = next(n for n in nodes if n != wid)
            wheel = components[wid]
            # WheelPair already contains a bearing and wheel body. Its carrier
            # frame is the wheel centre, not an arbitrary hub displacement.
            if any(abs(v) > 1e-12 for v in pose(wheel['ports']['hub']['pose'])):
                raise ValueError('UNSUPPORTED: noncentral or rotated wheel hub')
            bearings[wid] = {'input_joint': joint['id'], 'carrier': carrier_id,
                             'mount': transform(pose(components[carrier_id]['pose']), pose(wheel['pose'])),
                             'lowering': 'absorbed_by_WheelPair_bearing',
                             'axis': [0, -1, 0], 'additional_revolutes': 0,
                             'initial_relative_roll_rad': 0,
                             'initial_relative_roll_speed_rad_s': 0}
            continue
        ja = world_port(pose(joint['pose']), pose(joint['ports']['a']['pose']))
        jb = world_port(pose(joint['pose']), pose(joint['ports']['b']['pose']))
        # At this revision a joint's two frames must be colocated and aligned;
        # body-side port offsets and rotations remain fully supported.
        if any(abs(a-b) > 1e-12 for a, b in zip(ja, jb)):
            raise ValueError('UNSUPPORTED: noncoincident joint frames')
        connections.append({'input_joint': joint['id'], 'input_connections': sorted(
            c['id'] for c in ir['connections'] if any(e.startswith(joint['id']+'.') for e in (c['from'], c['to']))),
            'kind': joint['kind'], 'body_a': nodes[0], 'body_b': nodes[1],
            'a_to_joint': transform(pose(components[nodes[0]]['pose']), ja),
            'joint_to_b': transform(jb, pose(components[nodes[1]]['pose'])),
            'joint_class': None if joint['kind'] == 'fixed' else 'Modelica.Mechanics.MultiBody.Joints.Revolute',
            'axis': [0, -1, 0], 'initial_phi_rad': 0, 'initial_speed_rad_s': 0,
            'physical_joint_count': joint['count'], 'representative_joint_count': 1})
    bodies = [{'input_id': c['id'], 'class': 'V11PhysicalB1.RigidBody2D',
               'initial_com_pose': list(pose(c['pose'])), **component_mass(c)}
              for c in sorted(design['bodies'], key=lambda c: c['id'])]
    wheels = [{'input_id': c['id'], 'class': 'V11PhysicalB1.WheelPair',
               'radius_m': c['radius']['value'], **component_mass(c),
               'mount_and_bearing': bearings[c['id']],
               'contact': {'class': 'V11PhysicalB1.PolylineWheelContact',
                           'law': 'unchanged_accepted_B', 'parameters': 'REQUIRES_C_PHYSICAL_DESCRIPTOR'},
               'shaft_boundary': 'drive_required' if c['driven'] else 'free_zero_torque',
               'absolute_roll_speed': 'minus_wheel_body_pitch_rate'}
              for c in sorted(design['wheels'], key=lambda c: c['id'])]
    mounts, drives = [], []
    for c in sorted([*design['drives'], design['power_supply']], key=lambda c: c['id']):
        parent = components[c['mount_body']]
        mounts.append({'input_id': c['id'], 'class': 'V11PhysicalB1.RigidBody2D',
                       'mounted_on': parent['id'], 'mount': transform(pose(parent['pose']), pose(c['pose'])),
                       **component_mass(c), 'mass_semantics': 'separate_rigid_hardware_mass_not_rotor_inertia'})
        if c['kind'] == 'drive_pair':
            wheel_id = peers[c['id']+'.shaft'][0].split('.')[0]
            drives.append({'input_id': c['id'], 'wheel_id': wheel_id,
                           'class': 'V11PhysicalB1.IdealDriveSupply',
                           'ratedTorque_Nm': c['count']*c['torque']['value'],
                           'ratedMechanicalPower_W': c['count']*c['power']['value'],
                           'efficiency': c['efficiency']['value'], 'voltage_V': c['voltage']['value'],
                           'shaft_connect': wheel_id+'.axis', 'support_connect': wheel_id+'.support',
                           'controller': 'REQUIRES_C_PHYSICAL_DESCRIPTOR',
                           'branch_energy_is_not_shared_battery_energy': True})
    masses = bodies + wheels + mounts
    total = sum(c['m_kg'] for c in masses)
    if not math.isclose(total, ir['mass_accounting']['total_loaded_mass_kg'], abs_tol=1e-9):
        raise ValueError('MASS_LOWERING_MISMATCH')
    com = [sum(c['m_kg']*pose(components[c['input_id']]['pose'])[k] for c in masses)/total for k in (0, 1)]
    inertia = sum(c['J_kg_m2']+c['m_kg']*sum((pose(components[c['input_id']]['pose'])[k]-com[k])**2 for k in (0, 1)) for c in masses)
    result = {'version': VERSION, 'status': 'BINDING_PREVIEW_ONLY', 'executable': False,
              'approval_status': 'PENDING_CONCRETE_C_CONFIRMATION',
              'project_id': design['project_id'], 'baseline_version_id': design['baseline_version_id'],
              'design_digest': design['design_digest'], 'structure_topology_digest': ir['topology_digest'],
              'accepted_b_source_sha256': require_b_source(),
              'root': {'body': chassis['id'], 'class': 'Modelica.Mechanics.MultiBody.Joints.Planar',
                       'n': [0,-1,0], 'n_x': [1,0,0], 'floating_dof': ['x','z','pitch'],
                       'pitch_lock': False, 'initial_pose': list(pose(chassis['pose'])),
                       'initial_speeds': 'REQUIRES_C_PHYSICAL_DESCRIPTOR'},
              'bodies': bodies, 'wheels': wheels, 'joints': connections,
              'hardware_mass_mounts': mounts, 'drives': drives,
              'shared_supply': {'input_id': design['power_supply']['id'],
                  'rated_power_W': design['power_supply']['power']['value'],
                  'usable_energy_J': design['power_supply']['usable_energy']['value'],
                  'required_global_equation': 'Pbus=sum(branch.dcPower)+Paux; der(Ebus)=-Pbus',
                  'required_assertions': ['Pbus<=supply_rating', 'Ebus>=0'],
                  'status': 'C_AGGREGATE_IMPLEMENTATION_REQUIRED'},
              'mass_check': {'total_kg': total, 'payload_kg': ir['mass_accounting']['payload_kg'],
                             'vehicle_without_payload_kg': total-ir['mass_accounting']['payload_kg'],
                             'initial_com_xz_m': com, 'initial_inertia_about_com_kg_m2': inertia},
              'trace_map': deepcopy(compiled['trace_map']),
              'unverified': ['actual Modelica generation and compile', 'initialization and static equilibrium',
                            'body collision envelopes', 'multiwheel contact dynamics',
                            'shared power and control', 'whole vehicle physics and numerical accuracy']}
    result['preview_digest'] = digest(result)
    return result
