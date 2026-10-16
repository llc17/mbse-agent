"""Explicit B fixture descriptions and A component coordinate/accounting adapters.

This module neither changes the A registry nor approves a product. It has no
network, environment-file, Modelica execution, or legacy candidate imports.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / '任务规划/B-基础物理组件与验证协议.md'
PROTOCOL_SHA = 'cc9875ad68b44aa1d614e54fa16226165d527b63f8b052de9ef2b0efb8f13de8'
VERSION = 'v11-physical-b1'
MAP_VERSION = 'v11-a-to-b-map-1'
BASELINE = 'ab8f0e3f2e35b661f89d62415ed61773be91b145'
CLASS_NAMES = dict(zip(('B01', 'B02', 'B03', 'B04', 'B05', 'B06'),
                      ('B01PassiveJoint', 'B02DropStatic', 'B03DriveBrake',
                       'B04RiserImpact', 'B05CornerImpact', 'B06RollOff')))


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                       allow_nan=False) + '\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('INVALID: finite numeric value required')
    return float(value)


def world_port(pose, local):
    x, z, p = map(finite, pose)
    u, v, q = map(finite, local)
    return (x + math.cos(p)*u - math.sin(p)*v,
            z + math.sin(p)*u + math.cos(p)*v, p+q)


def msl_frame_wrench(fx, fz, pitch_moment):
    """All inputs already resolved in the world frame; pitch is positive about -y."""
    return (finite(fx), 0.0, finite(fz)), (0.0, -finite(pitch_moment), 0.0)


def shaft_mapping(roll_relative, speed_relative, roll_torque, carrier_pitch_rate):
    """The contact law needs absolute +y speed; shaft power needs relative speed."""
    q, w, tau, carrier = map(finite, (roll_relative, speed_relative, roll_torque, carrier_pitch_rate))
    return {'joint_q': -q, 'joint_w': -w, 'joint_torque': -tau,
            'absolute_roll_speed': w-carrier, 'shaft_power': tau*w,
            'carrier_pitch_reaction': tau}


def aggregate_component(*, count, mass, inertia, radius=None, torque=None, power=None,
                        stiffness=None, damping=None, efficiency=1.0):
    if type(count) is not int or count not in (1, 2):
        raise ValueError('UNSUPPORTED: single component or symmetric pair required')
    mass, inertia, efficiency = map(finite, (mass, inertia, efficiency))
    if mass <= 0 or inertia <= 0 or not 0 < efficiency <= 1:
        raise ValueError('INVALID: mass/inertia/efficiency')
    result = {'mass': count*mass, 'inertia_yy': count*inertia, 'count_applied': count,
              'inertia_xx': count*inertia/2, 'inertia_zz': count*inertia/2,
              'dimensional_scope': 'constrained_planar_only'}
    for name, value in (('torque', torque), ('mechanical_power', power),
                        ('stiffness', stiffness), ('damping', damping)):
        if value is not None:
            value = finite(value)
            if value < 0:
                raise ValueError('INVALID: negative rating')
            result[name] = count*value
    if radius is not None:
        result['radius'] = finite(radius)
        if result['radius'] <= 0:
            raise ValueError('INVALID: radius')
    if power is not None:
        result['electrical_rating'] = count*power/efficiency
    return result


def mount_offset(parent_pose, component_pose):
    px, pz, pp = map(finite, parent_pose)
    x, z, p = map(finite, component_pose)
    return (math.cos(pp)*(x-px)+math.sin(pp)*(z-pz),
            -math.sin(pp)*(x-px)+math.cos(pp)*(z-pz), p-pp)


def terrain_segments(points):
    """Canonical directed, finite stair boundary: increasing x, solid on right/below."""
    points = [tuple(map(finite, point)) for point in points]
    if len(points) < 2 or any(len(point) != 2 for point in points):
        raise ValueError('INVALID: polyline')
    result = []
    for i, ((ax, az), (bx, bz)) in enumerate(zip(points, points[1:])):
        dx, dz = bx-ax, bz-az
        if dx < 0 or (dx == 0 and dz == 0) or (dx != 0 and dz != 0):
            raise ValueError('UNSUPPORTED: monotone-x axis-aligned nonzero stair segment required')
        length = math.hypot(dx, dz)
        result.append({'id': i+1, 'a': [ax, az], 'b': [bx, bz],
                       'normal': [-dz/length, dx/length], 'length': length})
    return result


def corner_kinds(points):
    segments = terrain_segments(points)
    corners = []
    for a, b in zip(segments, segments[1:]):
        da = (a['b'][0]-a['a'][0], a['b'][1]-a['a'][1])
        db = (b['b'][0]-b['a'][0], b['b'][1]-b['a'][1])
        cross = da[0]*db[1]-da[1]*db[0]
        corners.append({'point': a['b'], 'kind': 'convex' if cross < 0 else 'concave' if cross > 0 else 'collinear'})
    return corners


def fixture(ident):
    """Only six frozen development inputs; unknown cases never fall back to B06."""
    if ident not in CLASS_NAMES:
        raise ValueError('UNSUPPORTED: fixture')
    data = {'schema': 'v11-b-fixture-1', 'project_id': 'v11-s3-b-component-dev',
            'baseline_version_id': BASELINE, 'physical_version': VERSION,
            'mapping_version': MAP_VERSION, 'protocol_file_sha256': PROTOCOL_SHA,
            'purpose': 'development_fixture', 'product_approval': False,
            'ident': ident, 'model_class': 'V11PhysicalB1.'+CLASS_NAMES[ident],
            'pose_is_com': True, 'inertia_reference': 'component_com_out_of_plane',
            'planar_axis': [0, -1, 0], 'rolling_axis': [0, 1, 0],
            'initial_carrier_pitch': 0, 'auxiliary_power': 0, 'rolling_resistance': 0,
            'source': 'ai_development_assumption_not_user_vehicle'}
    if ident == 'B01':
        data['parameters'] = {'rod_mass': 2, 'rod_length': .5, 'rod_inertia': 2*.5**2/12,
                              'load_mass': 1, 'load_inertia': .0025, 'theta0': .02,
                              'omega0': 0, 'gravity': 9.81, 'joint_damping': 0}
        data['stop'] = 4.0
    else:
        p = {'physical_count': 2, 'mass_per_wheel': 1, 'inertia_per_wheel': .005,
             'radius': .1, 'stiffness_per_wheel': 50000, 'damping_per_wheel': 100,
             'vreg': .001, 'gravity': 9.81, 'mu': 0, 'wheel_omega0': 0}
        if ident == 'B02':
            p.update(x0=0, z0=.12, vx0=0, vz0=0, terrain=[[-2, 0], [2, 0]])
            data['stop'] = 4.0
        elif ident == 'B03':
            p.update(x0=0, z0=.1-19.62/100000, vx0=0, vz0=0, mu=.6,
                     terrain=[[-2, 0], [4, 0]], motor_torque_per_wheel=.1,
                     motor_power_per_wheel=2, voltage=24, efficiency=.8,
                     supply_power_limit=10, usable_energy=100, regeneration=False,
                     torque_time_table=[[0, 0], [.1, .2], [1.4, .2], [1.5, -.2], [2.4, -.2], [2.5, 0], [3, 0]])
            data['stop'] = 3.0
        elif ident in ('B04', 'B05'):
            p.update(gravity=0, terrain=[[-1, 0], [0, 0], [0, .3], [1, .3]])
            if ident == 'B04':
                p.update(x0=-.15, z0=.15, vx0=.2, vz0=0)
            else:
                p.update(x0=-.15/math.sqrt(2), z0=.3+.15/math.sqrt(2),
                         vx0=.2/math.sqrt(2), vz0=-.2/math.sqrt(2))
            data['stop'] = 1.0
        else:
            p.update(x0=-.2, z0=.18-19.62/100000, vx0=2, vz0=0, wheel_omega0=20,
                     mu=.6, terrain=[[-1, .08], [0, .08], [0, 0], [2, 0]])
            data['stop'] = .6
        data['parameters'] = p
        data['segments'] = terrain_segments(p['terrain'])
        data['corners'] = corner_kinds(p['terrain'])
    data['descriptor_digest'] = digest(data)
    return data


def require_frozen_fixture(descriptor, *, product=False):
    if product or descriptor.get('purpose') != 'development_fixture' or descriptor.get('product_approval') is not False:
        raise ValueError('UNSUPPORTED: B development descriptor is not product approval')
    expected = fixture(descriptor.get('ident'))
    if canonical(descriptor) != canonical(expected):
        raise ValueError('STALE: changed fixture requires a revised concrete proposal')
    if hashlib.sha256(PROTOCOL.read_bytes()).hexdigest() != PROTOCOL_SHA:
        raise ValueError('STALE: frozen protocol changed')
    return deepcopy(descriptor)


def require_dependency_hashes(rows, *, base=ROOT.parent):
    for row in rows:
        path = Path(row['path'])
        if not path.is_absolute():
            path = base / path
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('DEPENDENCY_BLOCKED: '+str(path))


def write_fixtures(directory):
    """Exclusive non-executable preparation artifacts; does not start Modelica."""
    directory = Path(directory).resolve()
    if not directory.is_relative_to(ROOT / 'configs/component_physics'):
        raise ValueError('INVALID: fixture directory scope')
    directory.mkdir(parents=True, exist_ok=False)
    for ident in CLASS_NAMES:
        (directory / (ident+'.json')).write_bytes(canonical(fixture(ident)))
