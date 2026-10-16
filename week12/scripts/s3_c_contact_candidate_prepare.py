"""Prepare an isolated, unexecuted contact ownership repair candidate."""
import hashlib
from week12.src.integration.execution import ROOT,OUT,read,save,sha
from week12.src.composition.schema import digest

DEST=ROOT/'results/s3-c-evidence-review-20260926-01'
B=ROOT/'models/components/V11PhysicalB1/package.mo'
EXPECTED='0fbf2e80bb4e43f1a5bfcf382c42c8b4837f32b83628e17fc2e2f167bd8d774e'


def main():
    if sha(B)!=EXPECTED: raise ValueError('PROTECTED_B_DRIFT')
    source=B.read_text(encoding='utf-8')
    start=source.index('  model PolylineWheelContact\n'); end=source.index('  end PolylineWheelContact;',start)+len('  end PolylineWheelContact;')
    contact=source[start:end]
    edits=[('    Boolean faceActive[nSegments];',
            '    Boolean faceActive[nSegments];\n    Boolean afterStart[nSegments];\n    Boolean beforeEnd[nSegments];'),
           ('      faceActive[i] = projection[i] >= 0 and\n                      projection[i] < segmentLength[i] and\n                      not (vertexKind[i] == 1 and projection[i] <= 0) and',
            '      afterStart[i] = projection[i] > 0;\n      beforeEnd[i] = projection[i] < segmentLength[i];\n      faceActive[i] = (if vertexKind[i] == 1 then afterStart[i] else projection[i] >= 0) and\n                      beforeEnd[i] and'),
           ('        vertexNx[i]*nx[i - 1] + vertexNz[i]*nz[i - 1] >= 0 and\n        vertexNx[i]*nx[i] + vertexNz[i]*nz[i] >= 0;',
            '        not beforeEnd[i - 1] and not afterStart[i];')]
    for old,new in edits:
        if contact.count(old)!=1: raise ValueError('UNEXPECTED_SOURCE_FOR_CONTACT_PATCH')
        contact=contact.replace(old,new)
    # Axis-aligned monotone terrain is the reviewed equivalence domain. Do not
    # silently widen B's contact contract to arbitrary oblique polygons.
    marker='    terrainDefinedPrefix[1] = false;'
    guard='''    for j in 1:nSegments loop
      assert((xPoints[j + 1] == xPoints[j] or zPoints[j + 1] == zPoints[j]) and
             segmentLength[j] > Modelica.Constants.small,
             "C2 ownership repair supports axis-aligned terrain only");
    end for;
'''
    contact=contact.replace(marker,guard+marker)
    package='within;\npackage V11ContactC2\n  "Unvalidated C contact ownership candidate; accepted B1 is unchanged"\n  import SI = Modelica.Units.SI;\n  import MB = Modelica.Mechanics.MultiBody;\n'+contact+'\nend V11ContactC2;\n'
    path=ROOT/'models/components/V11ContactC2/package.mo'; path.parent.mkdir(exist_ok=False)
    path.write_text(package,encoding='utf-8',newline='\n')
    candidates=[]; fixtures=['within;','package V11ContactC2Probes','  import MB = Modelica.Mechanics.MultiBody;','  import Trans = Modelica.Mechanics.Translational;']
    depth=.0026312270386701087; distance=.127-depth
    for ident,axis,speed in [('T01',0,.4),('T02',0,-.4),('T03',2,.4),('T04',2,-.4)]:
        offset=[0.,0.,distance] if axis==0 else [-distance,0.,0.]
        n=[1,0,0] if axis==0 else [0,0,1]
        f=lambda vs:'{'+','.join(format(x,'.17g') for x in vs)+'}'
        fixtures += [f'  model {ident}',
            '    inner MB.World world(g=0, enableAnimation=false);',
            f'    MB.Parts.FixedTranslation anchor(r={f(offset)}, animation=false);',
            f'    MB.Joints.Prismatic slide(n={f(n)}, useAxisFlange=true, animation=false);',
            '    Trans.Sources.Position imposed(useSupport=true, exact=true);',
            '    V11ContactC2.PolylineWheelContact contact(R=.127,k=100000,c=200,mu=.85,vreg=.001,',
            '      nPoints=4,xPoints={-1,0,0,1},zPoints={-.15,-.15,0,0},vertexKind={0,-1,1,0});',
            '    output Real x=slide.frame_b.r_0[1]; output Real z=slide.frame_b.r_0[3];',
            '    output Real vx=der(x); output Real vz=der(z);',
            f'    output Real omega={speed:.17g}/.127;']
        for name in ('Fx','Fz','normal','penetration','slip','elasticEnergy','normalLoss','frictionLoss'):
            fixtures.append(f'    output Real {name}=contact.{name};')
        fixtures += ['    output Integer featureId=contact.featureId;', '    output Integer contactCount=contact.contactCount;',
            '  equation','    connect(world.frame_b,anchor.frame_a);','    connect(anchor.frame_b,slide.frame_a);',
            '    connect(slide.frame_b,contact.frame);','    connect(imposed.flange,slide.axis);',
            '    connect(imposed.support,slide.support);',f'    imposed.s_ref={speed:.17g}*(time-.005);',
            '    contact.omegaAbsolute=omega;',f'  end {ident};']
        candidates.append({'id':ident,'axis':'x' if axis==0 else 'z','speed_m_s':speed,'stop_time_s':.01,'boundary_time_s':.005,
                           'radius_m':.127,'nominal_overlap_m':depth,'fixed_normal_coordinate_m':distance})
    fixtures.append('end V11ContactC2Probes;')
    fixture=DEST/'V11ContactC2Probes.mo'; fixture.write_text('\n'.join(fixtures)+'\n',encoding='utf-8')
    protocol={'status':'DRAFT_SOURCE_ONLY_NOT_EXECUTION_AUTHORIZATION',
        'scope':'C contact continuity repair validation only; no vehicle changes or car simulations',
        'source_B_sha256':EXPECTED,'candidate_sha256':sha(path),'fixture_sha256':sha(fixture),
        'reuse':['MSL4.0.0 pinned Prismatic/Position/FixedTranslation','existing B force/energy equations unchanged',
                 'existing bounded tool runner, event-cache audit and B independent regression tolerances'],
        'change':'share Boolean ownership relations at convex boundaries; use complements for corner/face; restrict equivalence to axis-aligned terrain',
        'new_probes':candidates,
        'variants':{'base':{'solver':'dassl','tolerance':1e-6,'max_step_s':1e-5,'output_interval_s':1e-5},
                    'refined':{'solver':'dassl','tolerance':1e-7,'max_step_s':5e-6,'output_interval_s':1e-5}},
        'independent_oracle':'finite segment distance and convex vertex radial distance reconstructed from prescribed world position; compute all active geometric features and fixed k/c/mu law independently; all event rows retained',
        'probe_gates':{'geometry_absolute_m':5e-7,'force_absolute_N':.1,'force_relative':1e-5,
            'normal_must_remain_positive_N':5,'no_tension_N':-1e-8,'negative_loss_tolerance_W':1e-8,
            'state_base_refined_absolute_m':1e-7,'event_time_difference_s':2e-6,
            'note':'new local analytical fixtures, not a relaxation of any B/C criterion; equivalent adjacent owner ties allowed but no zero/double contact'},
        'regression':['B02','B03','B04','B05','B06'],
        'regression_policy':'new isolated copies of the affected fixtures explicitly bind C2; original B source/results stay untouched; use their frozen physical/numerical tolerances. Reuse unaffected B01 evidence.',
        'round_case_order':'T01..T04 each base/refined, then B02..B06 each base/refined',
        'budget_proposal':{'maximum_rounds':3,'scene_attempts_per_round':18,'maximum_scene_attempts':54,
            'local_checks_per_round':6,'maximum_local_checks':18,'local_timeout_s':10,
            'build_timeout_s':180,'simulation_timeout_s':180,'postprocess_timeout_s':60,
            'total_execution_wall_s':1800,'SysML':0,'identity_tool_probes':0,'product_online_calls':0,
            'old_batch_slots_carried_over':0,'status':'NEW_FINITE_BUDGET_PENDING_CONFIRMATION'},
        'stop':['first failure pauses dependent run','implementation repairs within3 complete rounds only',
                'no physical/tolerance changes to manufacture pass','dependency drift/unsafe recovery/budget exhausted stop'],
        'exit':'new component variant has evidence or accurately recorded failure; does not certify vehicle climb or finish C',
        'pending_implementation':['dedicated runner/receipt and ledger for this new package','independent probe verifier','six meaningful local preflight groups','explicit C2 binding of isolated B regressions'],
        'not_authorized':['new vehicle mass/geometry/torque','body-envelope amendment','C full vehicle revalidation','D/E','V12']}
    protocol['proposal_digest']=digest(protocol)
    save(DEST/'contact-repair-proposal.json',protocol)
    save(DEST/'candidate-provenance.json',{'source':str(B.relative_to(ROOT)),'source_sha256':EXPECTED,
         'candidate':str(path.relative_to(ROOT)),'candidate_sha256':sha(path),'edits':[{'before':a,'after':b} for a,b in edits],
         'additional_change':'axis-aligned terrain assertion','status':'UNCOMPILED_UNSIMULATED_UNREGISTERED',
         'force_energy_law_changes':False,'public_B_modified':False})
    print(protocol['proposal_digest'])


if __name__=='__main__': main()
