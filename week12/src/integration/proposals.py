"""Two concrete, pending-review C proposals. No model compilation or execution."""
from copy import deepcopy
import math

from week12.src.composition.examples import quantity as q, pose, port, component
from week12.src.composition.schema import (
    SCHEMA_VERSION, GENERATOR_VERSION, load_library, library_digest, seal_design, digest)
from week12.src.integration.preview import binding_preview, VERSION as BINDING_VERSION

USER_FACTS = {'message': '1.总质量85kg  2.完整流程', 'date': '2026-09-24',
              'loaded_mass_kg': 85, 'payload_mass_kg': 20, 'riser_m': .15, 'tread_m': .28,
              'steps': 20, 'preliminary_steps': 3, 'direction': 'forward_front_wheels_first'}


def proposal(topology):
    if topology not in ('fixed_nose', 'rocker_bogie'):
        raise ValueError('Unknown C proposal topology')
    fixed = topology == 'fixed_nose'
    bodies, wheels, joints, drives, connections, envelopes = [], [], [], [], [], {}

    def body(cid, mass, inertia, x, z, role, count=1):
        c=component(cid,'rigid_body',count,mass,inertia,x,z,{},role=role)
        bodies.append(c)
        return c

    def box(cid, x0, x1, z0, z1):
        envelopes.setdefault(cid, []).append({'type':'polygon', 'vertices':[[x0,z0],[x1,z0],[x1,z1],[x0,z1]]})

    def rod(cid, start, end, radius):
        envelopes.setdefault(cid, []).append({'type':'capsule', 'start':start,'end':end,'radius':radius})

    def by_id(cid):
        return next(c for c in bodies+wheels if c['id']==cid)

    def link(jid, kind, aid, bid, x, z, count=1):
        for cid, side in ((aid,'a'),(bid,'b')):
            c=by_id(cid)
            pname='hub' if c['kind']=='tire_pair' else jid
            c['ports'][pname]=port(x=x-c['pose']['x']['value'],z=z-c['pose']['z']['value'])
            connections.append({'id':jid+'_'+side,'from':cid+'.'+pname,'to':jid+'.'+side,'source':'ai_design_parameter'})
        joints.append(component(jid,kind,count,0,0,x,z,{'a':port(),'b':port()},
                                dof=[] if kind=='fixed' else ['ry'],axis='y'))

    if fixed:
        # This x balances the explicit component first moments to x_COM=0.
        cx=-.18/22
        body('chassis',22,1.2,cx,.205,'chassis')
        body('payload',20,20*(.26**2+.24**2)/12,-.04,.40,'payload')
        body('controller',1,1*(.1**2+.08**2)/12,0,.32,'controller')
        carriers=[('rear','chassis',-.23,.127,.127,3,2,30,250),
                  ('front','chassis',.22,.127,.127,3,2,30,250),
                  ('nose','chassis',.54,.19,.07,1,1,12.5,120)]
        battery_pose=(-.06,.275)
        box('chassis',-.28,.28,-.055,.045)
        rod('chassis',[.22-cx,0],[.54-cx,-.015],.015)
        rod('chassis',[0,0],[-.04-cx,.195],.015)
    else:
        body('chassis',18,18*(.50**2+.10**2)/12,.07,.29,'chassis')
        # Balance the sprung subsystem horizontally below its free pivot.
        body('payload',20,20*(.26**2+.24**2)/12,-.063,.38,'payload')
        body('controller',1,1*(.1**2+.08**2)/12,0,.34,'controller')
        body('rocker',1,1*(.85**2+.04**2)/12,.07,.37,'link',2)
        body('bogie',1,1*(.50**2+.04**2)/12,-.30,.34,'link',2)
        link('rocker_pivot','passive_revolute','chassis','rocker',0,.46,2)
        link('bogie_pivot','passive_revolute','rocker','bogie',-.30,.34,2)
        carriers=[('rear','bogie',-.55,.24,.24,2,2,30,250),
                  ('middle','bogie',-.05,.24,.24,2,2,30,250),
                  ('front','rocker',.50,.24,.24,2,2,30,250)]
        battery_pose=(0,.24)
        box('chassis',-.25,.25,-.05,.05)
        rod('chassis',[-.07,0],[-.07,.17],.02)
        rod('rocker',[-.07,.09],[.43,-.13],.02)
        rod('rocker',[-.07,.09],[-.37,-.03],.02)
        rod('bogie',[-.25,-.10],[.25,-.10],.02)
        rod('bogie',[0,-.10],[0,0],.02)
    box('payload',-.13,.13,-.12,.12)
    box('controller',-.05,.05,-.04,.04)
    for c in bodies:
        if c['role'] in ('payload','controller'):
            link(c['id']+'_mount','fixed','chassis',c['id'],c['pose']['x']['value'],c['pose']['z']['value'])
    next(c for c in bodies if c['id']=='payload')['mass']['source']='user_requirement'
    for wid,carrier,x,z,r,wm,dm,tau,power in carriers:
        wheels.append(component(wid,'tire_pair',2,wm,.5*wm*r*r,x,z,
                                {'hub':port(),'shaft':port('rotational1d')},radius=q(r,'m'),driven=True))
        link(wid+'_bearing','wheel_revolute',carrier,wid,x,z,2)
        did=wid+'_drive'
        drives.append(component(did,'drive_pair',2,dm,dm*(.10**2+.10**2)/12,x,z,
                                {'shaft':port('rotational1d'),'dc':port('dc')},mount_body=carrier,
                                torque=q(tau,'N*m'),power=q(power,'W'),voltage=q(48,'V'),efficiency=q(.8,'1','ai_assumption')))
        box(did,-.05,.05,-.05,.05)
        connections.extend([{'id':did+'_shaft','from':did+'.shaft','to':wid+'.shaft','source':'ai_design_parameter'},
                            {'id':did+'_dc','from':'battery.dc','to':did+'.dc','source':'ai_design_parameter'}])
    battery=component('battery','dc_supply',1,18,18*(.30**2+.15**2)/12,*battery_pose,
                      {'dc':port('dc')},mount_body='chassis',voltage=q(48,'V'),power=q(2160,'W'),
                      usable_energy=q(48*60*3600*.9*.94,'J','ai_assumption'))
    box('battery',-.15,.15,-.075,.075)
    scenario={}
    for name in ('ascent','descent','flat'):
        scenario[name]={'speed':q(.4,'m/s','ai_assumption'),'initial_speed':q(0,'m/s','ai_assumption'),
                        'friction':q(.85,'1','ai_assumption'),'platform_length':q(2,'m','ai_assumption'),
                        'entry':{'ascent':'flat_before_first_riser','descent':'top_before_first_riser','flat':'level_ground'}[name],
                        'exit':'continue_cruise' if name=='flat' else 'last_wheel_leaves_steps_then_continue_cruise'}
        if name!='flat':
            scenario[name].update(riser=q(.15,'m','user_requirement'),tread=q(.28,'m','user_requirement'),steps=q(20,'1','user_requirement'))
    library=load_library()
    design=seal_design({'schema_version':SCHEMA_VERSION,'design_id':'c_'+topology,'revision':'proposal_1',
            'project_id':'v11_s3_c_integration','baseline_version_id':'user_4w2n_plus_accepted_b_20260924',
            'generator_version':GENERATOR_VERSION,'library_version':library['library_version'],'library_digest':library_digest(library),
            'representation':{'coordinates':'world_xz_pitch_y','parameter_basis':'per_physical_instance',
                              'pair_motion':'synchronized_left_right','aggregation':'multiply_by_count_once'},
            'requirements':{'profile':'V11','payload_mass':q(20,'kg','user_requirement'),
                            'descent_mode':'forward_front_wheels_first','motion_scope':'symmetric_planar',
                            'exit_behavior':'continue_cruise','wheel_type':'ordinary_tire'},
            'bodies':bodies,'wheels':wheels,'joints':joints,'drives':drives,'power_supply':battery,
            'connections':connections,'scenario':scenario,
            'provenance':{'origin':'user_model_adaptation_with_explicit_new_mass_and_dynamics' if fixed else 'ai_proposed_development_comparator',
                          'reference':'user supplied source-v1 full mission; 2026-09-24 total mass and scenario clarification',
                          'scope':'PENDING concrete approval; dimensions/mass split/inertia/control extensions are proposals, not simulation results'}})
    preview=binding_preview(design)
    physical={'schema':'v11-c-physical-proposal-1','status':'PENDING_USER_CONFIRMATION', 'executable':False,
              'user_facts':deepcopy(USER_FACTS),'design_digest':design['design_digest'],
              'binding_version':BINDING_VERSION,'b_source_sha256':preview['accepted_b_source_sha256'],
              'gravity_m_s2':9.81,'width_m':.64 if fixed else .70,
              'mass_kg':85,'vehicle_without_payload_kg':65,
              'inertia_policy':'Explicit local component inertias plus parallel-axis terms; original lumped I_pitch=20 is reference only',
              'envelopes_local_xz':envelopes,
              'contact_per_physical_wheel':{'stiffness_N_m':50000,'damping_Ns_m':100,'friction':.85,'vreg_m_s':.001,
                                           'source':'B stiffness/damping, source-v1 friction assumption; not tire calibration'},
              'wheel_pair_lowering':{'count':2,'stiffness_N_m':100000,'damping_Ns_m':200,
                                     'normal_force_semantics':'B contact.normal is already summed across the physical pair',
                                     'rolling_torque_pair':'-Crr*R*normal_pair*tanh(omega_absolute/0.1); no extra count factor'},
              'rolling_resistance':{'coefficient':.05,'omega_regularization_rad_s':.1,
                                    'law':'Mrr=-Crr*R*sum(Fn)*tanh(omega_absolute/0.1)',
                                    'reaction':'external terrain couple; not wheel-carrier internal brake',
                                    'loss':'-Mrr*omega_absolute >=0','when_no_contact':'Mrr=0',
                                    'source':'Crr from user energy reference, explicit regularized C extension'},
              'supply':{'voltage_V':48,'continuous_current_A':45,'continuous_power_W':2160,
                        'nominal_capacity_Ah':60,'usable_energy_J':battery['usable_energy']['value'],
                        'auxiliary_W':80,'regeneration':False,'mass_kg':18,
                        'source':'48V/60Ah/80W/0.9/0.94 from energy reference; 45A/18kg are unverified design assumptions',
                        'authority':'one shared energy account; branch B energy is redundant protection only'},
              'controller':{'type':'per-wheel proportional absolute-speed tracking with symmetric torque/power limits',
                            'kp_Nm_per_rad_s_per_wheel':12,'command_per_wheel':'clip(12*(v_ref/R-omega_absolute),-tau_cap,+tau_cap)',
                            'command_to_B_pair':'count*command_per_wheel; count=2; pair gain24 and pair ratings2*per-wheel ratings',
                            'tau_cap':'min(rated_torque, rated_mechanical_power/max(abs(omega_relative),1 rad/s))',
                            'initial_torque':0,'no_peak_overload':True,'no_position_actuator':True,
                            'settle_until_s':3,'ramp_duration_s':2,'cruise_m_s':.4},
              'initialization':{'reference':'design poses at nominal tire tangent plane',
                                'global_vertical_lift_m':.005,'vx_m_s':0,'vz_m_s':0,'pitch_rad':0,'pitch_rate_rad_s':0,
                                'passive_joint_angles_rad':0,'passive_joint_rates_rad_s':0,'relative_wheel_angles_rad':0,
                                'relative_wheel_rates_rad_s':0,'fixed_root_pitch':False,
                                'descent_vertical_offset':'steps*riser plus 0.005 lift',
                                'note':'3s free settling with zero drive; static gate must pass, no forced settling pose'},
              'passive_joint_damping':{} if fixed else {
                   'rocker_pivot':{'per_physical_joint_Nms_rad':10,'pair_Nms_rad':20},
                   'bogie_pivot':{'per_physical_joint_Nms_rad':1,'pair_Nms_rad':2}},
              'passive_damping_implementation':'MSL Rotational.Components.Damper across each Revolute axis/support; dissipated power=d*relative_rate^2, included in mechanical balance; no angle servo or hidden lock',
              'provenance_notes':{'mass_split':'AI budget satisfying user85kg including20kg; not weighed hardware',
                   'torque':('source main60/nose25 interpreted as pair total; proposed continuous minimum, no verified vendor curve' if fixed else 'AI comparator: each pair60Nm; borrowed magnitude, not a user original rocker specification; no verified vendor curve'),
                   'geometry':'fixed nose keeps original axle geometry; body COM/envelopes/inertias newly explicit; rocker is independent proposal'},
              'limitations':['2D symmetric motion, no differential/3D turning','no self collision proof or real tire calibration',
                             'no motor thermal or battery voltage sag model','not vendor or real-world certification']}
    physical['cases']=cases(topology,design)
    physical['physical_digest']=digest(physical)
    return {'design':design,'physical':physical,'binding':preview}


def cases(topology, design):
    result=[]
    for name in ('flat_brake','three_up','three_down','twenty_up','twenty_down','flat_energy'):
        steps=3 if name.startswith('three') else 20 if name.startswith('twenty') else 0
        direction='ascent' if name.endswith('_up') else 'descent' if name.endswith('_down') else 'flat'
        stop=20 if name=='flat_brake' else 35 if name=='flat_energy' else 60 if steps==20 else 35
        schedule=[[0,0],[3,0],[5,.4],[12,.4],[14,0],[20,0]] if name=='flat_brake' else [[0,0],[3,0],[5,.4],[stop,.4]]
        entry=2.; edge=entry+steps*.28
        # Full horizontal last tread is included; travel continues after all
        # wheel disks AND body envelopes have passed it by 0.5m.
        terrain=[[-2.,steps*.15 if direction=='descent' else 0.]]
        if steps:
            z=steps*.15 if direction=='descent' else 0.
            for i in range(steps):
                x=entry+i*.28
                terrain.append([x,z]); z+=-.15 if direction=='descent' else .15
                terrain.append([x,z])
            terrain.append([edge,z]); terrain.append([edge+30,z])
        else:
            terrain.append([35.,0.])
        result.append({'id':topology+'_'+name,'task':name,'direction':direction,'steps':steps,
                       'riser_m':.15,'tread_m':.28,'first_riser_x_m':entry,'exit_plane_x_m':edge if steps else None,
                       'exit_margin_m':.5,'terrain_points':terrain,'stop_time_s':stop,
                       'reference_speed_table_s_m_s':schedule,'initial_vertical_offset_m':steps*.15+.005 if direction=='descent' else .005,
                       'steady_window_s':[15,35] if name=='flat_energy' else None,
                       'requires':['shared_supply_probe','rolling_resistance_probe'] + ([] if name=='flat_brake' else [topology+'_flat_brake']) +
                           ([topology+'_three_up',topology+'_three_down'] if steps==20 else []),
                       'numeric_variants':['base','refined']})
    return result
