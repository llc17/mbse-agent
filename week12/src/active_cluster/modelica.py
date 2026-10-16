"""Graph lowering to actual MSL connections, scoped scene metadata and raw-output contract."""
from __future__ import annotations
import math

SCENES={"active_joint_energy":("Probe",12,0,False),"one_twin_module_load_cycle":("Module",120,1,False),"flat_energy_and_brake":("Flat",40,0,False),"three_up":("ThreeUp",600,3,False),"three_forward_down":("ThreeForwardDown",600,3,True),"twenty_up":("TwentyUp",2400,20,False),"twenty_forward_down":("TwentyForwardDown",2400,20,True)}

def num(x):
    return format(float(x),".17g")

def scalar_atom(x):
    """Operand for arithmetic composition; plain num stays valid in arrays."""
    return "("+num(x)+")"

def arr(xs):
    return "{"+",".join(arr(x) if isinstance(x,(list,tuple)) else num(x) for x in xs)+"}"

def flight_plan(design,n):
    g=design["geometry"]; H,D,r,rho,B,t=g["H"],g["D"],g["r"],g["carrier_radius"],g["fore_aft_hub_distance"],g["tBase"]
    theta=math.atan2(H,D); pi=math.pi; stages=[]
    def add(code,qf,qr,pf,pr,sf,sr,events,c=0,guard=None):
        stages.append(dict(code=code,qF=qf,qR=qr,pF=pf,pR=pr,sF=sf,sR=sr,events=events,c=c,guard=guard))
    # Reference pivots are virtual rolling targets, never constraints.
    add(0,0,0,[t-D,r],[t-D-B,r],1,1,[-0.5,0])
    add(1,0,0,[t-D,r],[0,r],1,1,[0,theta,2*theta,pi-theta],guard=("F_B",1))
    add(2,theta-pi,0,[t,H+r],[0,r],2,1,[0,theta,2*theta,pi],guard=("F_A",2))
    add(3,theta-2*pi,0,[t+D,2*H+r],[0,r],1,1,[0,theta])
    add(4,-2*pi,0,[t+D,2*H+r],[t-D,r],1,1,[0,theta,pi-theta],guard=("R_B",1))
    # At capture F is on tread3, R on tread1; same true discrete contact cycle thereafter.
    landedF=3; landedR=1; qf=theta-3*pi; qr=theta-pi
    while landedF<n:
        sf=1 if landedF%2==0 else 2; sr=1 if landedR%2==0 else 2
        add(5,qf,qr,[t+(landedF-1)*D,landedF*H+r],[t+(landedR-1)*D,landedR*H+r],sf,sr,[0,theta,2*theta,pi],guard=("F_"+("B" if sf==1 else "A"),landedF+1))
        landedF+=1;landedR+=1;qf-=pi;qr-=pi
    sf=1 if n%2==0 else 2; sr=1 if (n-2)%2==0 else 2
    add(5,qf,qr,[t+(n-1)*D,n*H+r],[t+(n-3)*D,(n-2)*H+r],sf,sr,[0,theta])
    qf-=theta; qr-=theta
    uc=design["path"]["correction"]["uCritical"]-theta
    add(6,qf,qr,[0,n*H+r],[t+(n-3)*D,(n-2)*H+r],sf,sr,[0,uc,theta,pi-theta],guard=("R_"+("B" if sr==1 else "A"),n-1))
    qr-=pi-theta; sr=1 if (n-1)%2==0 else 2
    add(7,qf,qr,[0,n*H+r],[t+(n-2)*D,(n-1)*H+r],sf,sr,[0,theta,2*theta,pi],guard=("R_"+("B" if sr==1 else "A"),n))
    qr-=pi;sr=1 if n%2==0 else 2
    add(8,qf,qr,[0,n*H+r],[t+(n-1)*D,n*H+r],sf,sr,[0,theta])
    qr-=theta
    c=design["path"]["correction"]["cTotal"]
    a_end=rho-D/2
    advance=g["wheel_center_distance"]-(t+c+a_end)+r+0.02+0.5
    add(9,qf,qr,[0,n*H+r],[t+(n-1)*D,n*H+r],sf,sr,[0,advance],c=c+a_end)
    segments=[]; edge=0.0
    for st in stages:
        for j,(u0,u1) in enumerate(zip(st["events"],st["events"][1:])):
            dur=10.0 if st["code"]==0 else 30.0 if st["code"]!=9 else advance/0.1+3.5
            segments.append(dict(st,u0=u0,u1=u1,start=edge,end=edge+dur,duration=dur,
                                 transfer=st["guard"] if j==len(st["events"])-2 else None))
            edge+=dur
    return segments

def _geometry(st,u,d,n,down=False,module=False):
    g=d["geometry"];H,D,r,rho,B=g["H"],g["D"],g["r"],g["carrier_radius"],g["fore_aft_hub_distance"]
    theta=math.atan2(H,D);zc=0 if module else d["graph"]["bodies"][0]["com_local"][2]
    code=st["code"];qf=st["qF"]-u if code in (1,2,3,4,5) else st["qF"];qr=st["qR"]-u if code in (4,5,6,7,8) else st["qR"]
    sgf=-1 if st["sF"]==1 else 1;sgr=-1 if st["sR"]==1 else 1
    f=[st["pF"][0]+sgf*rho*math.cos(qf),st["pF"][1]+sgf*rho*math.sin(qf)]
    rear=[st["pR"][0]+sgr*rho*math.cos(qr),st["pR"][1]+sgr*rho*math.sin(qr)]
    if 1<=code<=3:rear=[f[0]-math.sqrt(B*B-(f[1]-r)**2),r]
    elif code>=6:f=[rear[0]+math.sqrt(B*B-((2 if module else n)*H+r-rear[1])**2),(2 if module else n)*H+r]
    delta=f[1]-rear[1];alpha=math.asin(delta/B)
    au=u+theta if code in (4,6) else u
    a=max(0,H/2*math.sin(au)-D/2*(1-math.cos(au)));b=zc*math.sin(alpha);c=st["c"]
    Fx=lambda z:math.sqrt(B*B-z*z)/2+zc*z/B
    dc=d["path"]["correction"]["DeltaCritical"];c1=Fx(dc)-Fx(1.5*H);c2=Fx(1.5*H)-Fx(.5*H)
    if code==6:c=Fx(dc)-Fx(delta) if delta<dc else 0
    elif code==7:c=c1+Fx(1.5*H)-Fx(delta) if u<=2*theta else c1+c2
    elif code==8:c=c1+c2+Fx(.5*H)-Fx(delta)
    elif code==9:a=b=0;c=st["c"]+u
    elif code==0:a=b=0;c=u
    f[0]+=a+b+c;rear[0]+=a+b+c
    if down:
        reflect=(n-1)*D;f,rear=[reflect-rear[0],rear[1]],[reflect-f[0],f[1]];qf,qr=math.pi-qr,math.pi-qf
    poses={}
    for group,hub,q in (("F",f,qf),("R",rear,qr)):
        for label,sign in (("A",1),("B",-1)):
            poses[group+"_"+label]=[hub[0]+sign*rho*math.cos(q),hub[1]+sign*rho*math.sin(q)]
    return poses

def _face_at(point,pts,r):
    for i,(a,b) in enumerate(zip(pts,pts[1:]),1):
        if b[0]>a[0] and abs(b[1]-a[1])<1e-10 and a[0]<=point[0]<b[0] and abs(point[1]-a[1]-r)<1e-8:
            return i
    return None

def _bind_transfers(plan,d,n,pts,down=False,module=False):
    """Analytic endpoints bind exact owner/finite face; no dynamic load assumption."""
    r=d["geometry"]["r"];H=d["geometry"]["H"]
    for st in plan:
        start=_geometry(st,st["u1"] if down else st["u0"],d,n,down,module)
        end=_geometry(st,st["u0"] if down else st["u1"],d,n,down,module)
        targets=[]
        for wid,point in end.items():
            if module and wid.startswith("R"):continue
            fi=_face_at(point,pts,r);old=_face_at(start[wid],pts,r)
            if fi is not None and fi!=old:
                other=wid[0]+("_B" if wid.endswith("A") else "_A")
                support=_face_at(start[other],pts,r)
                targets.append(dict(wheel=wid,face=fi,tread=round(pts[fi-1][1]/H),old_wheel=other,old_face=support))
        if len(targets)>2:raise ValueError("UNSUPPORTED_MULTI_OWNER_EVENT")
        st["targets"]=targets
    return plan

def terrain(n,H,D,down=False):
    if n==0:return [[-10,0],[10,0]],[0,0]
    points=[[-4,0],[0,0]];kinds=[0,-1]
    for j in range(n):
        points.append([j*D,(j+1)*H]);kinds.append(1)
        points.append([(j+1)*D,(j+1)*H]);kinds.append(-1 if j<n-1 else 0)
    points[-1]=[(n-1)*D+2,n*H]
    if down:
        edge=(n-1)*D
        points=[[edge-x,z] for x,z in reversed(points)]
        kinds=list(reversed(kinds))
    return points,kinds

def _hardware(d,body_ids):
    g=d["geometry"];rho=g["carrier_radius"];B=g["fore_aft_hub_distance"];items=[]
    for row in d["graph"]["bodies"][0]["primitives"][:4]:
        x,y,z=row["centre"];a,b,c=row["dimensions"]
        items.append(dict(id=row["id"]+"_solid",body_id="chassis",kind="polygon",vertices=[[x-a/2,0,z-c/2],[x+a/2,0,z-c/2],[x+a/2,0,z+c/2],[x-a/2,0,z+c/2]],y_range=[y-b/2,y+b/2],source_id=row["source_id"]))
    for group,hub in (("F",B/2),("R",-B/2)):
        if group+"_carrier" not in body_ids:continue
        items.append(dict(id=group+"_motor_solid",body_id="chassis",kind="circle",center=[hub,0,0],radius=.055,y_range=[.18,.202]))
        items.append(dict(id=group+"_brake_solid",body_id="chassis",kind="circle",center=[hub,0,0],radius=.04,y_range=[.202,.212]))
        items.append(dict(id=group+"_feed_solid",body_id=group+"_carrier",kind="circle",center=[0,0,0],radius=.04,y_range=[.208,.222]))
        items.append(dict(id=group+"_rod_solid",body_id=group+"_carrier",kind="capsule",endpoints=[[-rho,0,0],[rho,0,0]],radius=.015,y_range=[.217,.225]))
        for label,x in (("A",rho),("B",-rho)):
            wid=group+"_"+label
            items.append(dict(id=wid+"_stator_solid",body_id=group+"_carrier",kind="circle",center=[x,0,0],radius=.03,y_range=[.206,.224]))
            items.append(dict(id=wid+"_rotor_solid",body_id=wid,kind="circle",center=[0,0,0],radius=.04,y_range=[.24,.28]))
    return items

def _alias(lines,columns,semantic,expr,typ="Real"):
    name="o_"+semantic.replace(".","_").replace("-","_")
    lines.append("    output "+typ+" "+name+"="+expr+";")
    columns[semantic]=name
    return name

def _sampled_supervisor(nseg, down, stable_handoff=None, shared_reference=None):
    """One first-iteration capture and commit; left limits never read new stage.

    The independent clock token makes later event iterations retain the captured
    snapshot. Explicit pre-observation inputs avoid current commit feedthrough;
    snapshots, decisions and held outputs have one algorithm owner.
    """
    initial = nseg if down else 1
    fields = {
        'supervisorTick': ('Integer', '0'), 'snapSegment': ('Integer', str(initial)),
        'snapPaused': ('Boolean', 'true'), 'snapTracking': ('Boolean', 'false'),
        'snapSupport': ('Boolean', 'false'), 'snapSaturated': ('Boolean', 'false'),
        'snapFailed': ('Boolean', 'false'), 'snapFailureSeen': ('Boolean', 'false'),
        'snapOwner': ('Integer', '0'), 'snapOwner2': ('Integer', '0'),
        'snapForce': ('Real', '0'), 'snapForce2': ('Real', '0'),
        'snapClock': ('Real', '0'), 'snapLoadedSince': ('Real', '-1'),
        'snapLoad': ('Boolean', 'false'), 'snapGuard': ('Boolean', 'false'),
        'snapEndpoint': ('Boolean', 'false'), 'decisionTime': ('Real', '-1'),
        'stageEntryTime': ('Real', '0'), 'stageEntryCount': ('Integer', '0'),
    }
    if shared_reference is not None:
        fields['snapRate']=('Real','0')
    declarations = ['    discrete '+typ+' '+key+'(start='+start+',fixed=true);' for key,(typ,start) in fields.items()]
    declarations += [
        '    discrete Boolean failureSeen(start=false,fixed=true);',
        '    discrete Real failureTime(start=-1,fixed=true);',
        '    discrete Real lastFailureEventTime(start=-1,fixed=true);',
        '    discrete Integer failureCount(start=0,fixed=true);',
        '    discrete Real loadLossTime(start=-1,fixed=true);',
        '    discrete Real loadGainTime(start=-1,fixed=true);',
        '    discrete Integer loadLossCount(start=0,fixed=true);',
        '    discrete Integer loadGainCount(start=0,fixed=true);',
        '    Boolean pauseRaw;',
    ]
    endpoint = 'snapClock<=begin[snapSegment]' if down else 'snapClock>=edge[snapSegment]'
    advance = 'max(1,snapSegment-1)' if down else 'min('+str(nseg)+',snapSegment+1)'
    capture = {'snapSegment':'pre(segment)', 'snapPaused':'pre(paused)',
        'snapTracking':'pre(trackingOK)', 'snapSupport':'pre(oldSupportOK)',
        'snapSaturated':'pre(saturated)', 'snapFailed':'pre(failed)',
        'snapFailureSeen':'pre(failureSeen)', 'snapOwner':'guardOwner[snapSegment]',
        'snapOwner2':'guardOwner2[snapSegment]', 'snapForce':'pre(targetForce)',
        'snapForce2':'pre(targetForce2)', 'snapClock':'pre(referenceClock)',
        'snapLoadedSince':'pre(loadedSince)'}
    if shared_reference is not None:
        capture['snapRate']='pre(sharedPhaseRate)'
    algorithm = ['  algorithm', '    when initial() then', '      segment:='+str(initial)+';', '      paused:=true;']
    algorithm += ['      '+key+':='+start+';' for key,(_,start) in fields.items()]
    algorithm += ['    elsewhen sample(0.02,0.02) then',
        '      if pre(supervisorTick)<integer(floor(time/0.02+0.5)) then',
        '        supervisorTick:=integer(floor(time/0.02+0.5));']
    algorithm += ['        '+key+':='+expr+';' for key,expr in capture.items()]
    algorithm += [
        '        snapLoad:=(snapOwner==0 or snapForce>=40) and (snapOwner2==0 or snapForce2>=40);',
        '        snapGuard:=snapOwner==0 or (snapLoad and snapLoadedSince>=0 and time-snapLoadedSince>=0.2);',
        '        snapEndpoint:='+endpoint+';',
        '        decisionTime:=time;',
        '        segment:=snapSegment;',
        '        if snapFailureSeen or snapFailed then',
        '          paused:=true;',
        '        elseif snapEndpoint and snapGuard and snapLoad'+(' and snapTracking and snapSupport and not snapSaturated' if stable_handoff is not None else '')+(' and snapRate<=1e-10' if shared_reference is not None else '')+' then',
        '          segment:='+advance+';',
        '          paused:=segment<>snapSegment or not snapTracking or not snapSupport or snapSaturated;',
        '        else',
        '          paused:=not snapTracking or not snapSupport or snapSaturated or (snapEndpoint and not snapGuard);',
        '        end if;',
        '        stageEntryCount:=pre(stageEntryCount)+(if segment<>snapSegment then 1 else 0);',
        '        stageEntryTime:=if segment<>snapSegment then time else pre(stageEntryTime);',
        '      else',
        '        segment:=pre(segment); paused:=pre(paused);',
    ]
    algorithm += ['        '+key+':=pre('+key+');' for key in fields]
    algorithm += ['      end if;', '    end when;',
        '  algorithm',
        '    when initial() then failureSeen:=failed; failureTime:=if failed then time else -1; lastFailureEventTime:=if failed then time else -1; failureCount:=if failed then 1 else 0;',
        '    elsewhen failed then failureSeen:=true; failureTime:=if pre(failureSeen) then pre(failureTime) else time; lastFailureEventTime:=time; failureCount:=pre(failureCount)+1; end when;',
        '  algorithm',
        '    when initial() then loadedSince:=if loadOK then time else -1; loadLossCount:=0; loadGainCount:=0; loadLossTime:=-1; loadGainTime:=if loadOK then time else -1;',
        '    elsewhen {change(segment),change(loadOK)} then',
        '      loadedSince:=if loadOK then time else -1;',
        '      loadLossCount:=pre(loadLossCount)+(if not loadOK and pre(loadOK) then 1 else 0);',
        '      loadGainCount:=pre(loadGainCount)+(if loadOK and not pre(loadOK) then 1 else 0);',
        '      loadLossTime:=if not loadOK and pre(loadOK) then time else pre(loadLossTime);',
        '      loadGainTime:=if loadOK and not pre(loadOK) then time else pre(loadGainTime);',
        '    end when;']
    aliases = {key: (key, typ) for key,(typ,_) in fields.items()}
    aliases.update({key:(key,typ) for key,typ in (
        ('failureSeen','Boolean'), ('failureTime','Real'), ('lastFailureEventTime','Real'), ('failureCount','Integer'),
        ('loadLossTime','Real'), ('loadGainTime','Real'), ('loadLossCount','Integer'), ('loadGainCount','Integer'),
        ('loadedSince','Real'), ('pauseRaw','Boolean'), ('saturated','Boolean'), ('referenceClock','Real'), ('targetForce','Real'), ('targetForce2','Real'))})
    return declarations, algorithm, aliases


def _scene(design,name,class_name,stop,n,down,controller=None,correction=None,stable_handoff=None,shared_reference=None):
    bodies=design["graph"]["bodies"]; joints=design["graph"]["joints"]
    g=design["geometry"]; control=design["control"];physics=design["physics"]
    H,D,r,rho,B=g["H"],g["D"],g["r"],g["carrier_radius"],g["fore_aft_hub_distance"]
    theta=math.atan2(H,D);zc=design["graph"]["bodies"][0]["com_local"][2]
    probe=class_name=="Probe";module=class_name=="Module";flat=class_name=="Flat"
    # Module is an explicitly scoped load fixture; no fixture constraints enter vehicle scenes.
    actual_bodies=[b for b in bodies if not module or b["id"]=="chassis" or b["id"].startswith("F")]
    actual_joints=[j for j in joints if any(b["id"]==j["child"] for b in actual_bodies)]
    pn=0 if probe or flat else (1 if module else n)
    pts,kinds=terrain(pn,H,D,down)
    if probe:pts=[[-10,-10],[10,-10]]
    points=len(pts)
    plan=[] if probe or flat else flight_plan(design,max(3,n))
    if module:
        # One standalone full-pi cycle from a settled lower tread, 3 fixed30s quintic segments.
        plan=[dict(code=2,qF=theta-math.pi,qR=0,pF=[g["tBase"],H+r],pR=[0,r],sF=2,sR=1,u0=a,u1=b,start=i*30,end=(i+1)*30,duration=30,c=0,transfer=("F_A",2) if i==2 else None) for i,(a,b) in enumerate(zip([0,theta,2*theta],[theta,2*theta,math.pi]))]
        pts,kinds=terrain(2,H,D);points=len(pts)
    if plan:plan=_bind_transfers(plan,design,n,pts,down,module)
    sampled = bool(plan and controller is not None)
    elapsed=plan[-1]["end"] if plan else 0
    reflect=(n-1)*D if down else 0
    settle=1.0
    lead=g["tBase"]-D-0.5
    x0=lead-rho-B/2
    z0=r-design["mass_ledger"]["total"]*9.81/(8*physics["proposed_contact_parameters"]["per_physical_tyre_k"])
    qF0=qR0=phi0=0.0
    if down:
        final=plan[-1];c=g["tBase"]+final["c"]+final["u1"]
        rear_final_x=(n-1)*D+c-rho
        # Reflected ascent-rear maps to descending-front, full cruise reversal is preserved.
        x0=reflect-(rear_final_x+B/2);z0+=n*H
        qF0=math.pi-final["qR"];qR0=math.pi-final["qF"]
    if module:
        x0=g["tBase"]-rho*math.cos(theta);z0=H+r-rho*math.sin(theta)-42.5*9.81/(4*physics["proposed_contact_parameters"]["per_physical_tyre_k"])
        qF0=theta-math.pi
    lines=["  model "+class_name,"    import C=V11ActiveClusterC3;","    import MB=Modelica.Mechanics.MultiBody;","    import Frames=Modelica.Mechanics.MultiBody.Frames;","    inner MB.World world(gravityType=MB.Types.GravityTypes."+("NoGravity" if probe else "UniformGravity")+",g=9.81,n={0,0,-1},enableAnimation=false);",
      "    MB.Joints.Planar planar(n={0,-1,0},n_x={1,0,0},animation=false,s_x(start="+num(x0)+",fixed=true),s_y(start="+num(z0)+",fixed=true),v_x(start=0,fixed=true),v_y(start=0,fixed=true),phi(start=0,fixed=true),revolute(w(start=0,fixed=true)));"]
    columns={};body_contract=[];joint_contract=[];drive_contract=[];wheel_contract=[];features=[];transfers=[]
    smooth_drive=class_name=='ThreeForwardDown' and shared_reference is not None and shared_reference.get('smooth_carrier_drive') is True
    if smooth_drive:
        lines.append('    import D=V11ActiveClusterDriveSmooth;')
    if module:
        lines[-1]=lines[-1].replace("MB.Joints.Planar planar","V11PhysicalB1.PlanarWithPitchAxis planar").replace("phi(start=0,fixed=true),revolute(w(start=0,fixed=true))","phi(start=0,fixed=false),revolute(w(start=0,fixed=false))")
        lines.append("    Modelica.Mechanics.Rotational.Components.Fixed fixturePitch;")
    for b in actual_bodies:
        m=b["m"];I=b["inertia"];cm=b["com_local"]
        if module and b["id"]=="chassis":m/=2;I=[[v/2 for v in row] for row in I]
        lines.append("    C.Body "+b["id"]+"(m="+num(m)+",g="+("0" if probe else "9.81")+",rCM="+arr(cm)+",I="+arr(I)+");")
        row=dict(b,columns={},loads=[],actual_mass=m,actual_com_local=cm,actual_inertia=I)
        row.update(m=m,com_local=cm,inertia=I)
        for key in ("x","y","z","vx","vy","vz","ax","az","omega","alpha","kinetic","potential"):
            row["columns"][key]=_alias(lines,columns,"body."+b["id"]+"."+key,"0" if probe and key=="potential" else b["id"]+"."+key)
        expr="planar.phi" if b["id"]=="chassis" else "planar.phi+"+b["id"]+"_joint.phi" if b["kind"]=="carrier_pair" else "planar.phi+"+b["parent"]+"_joint.phi+"+b["id"]+"_joint.phi"
        row["columns"]["phi"]=_alias(lines,columns,"body."+b["id"]+".phi",expr)
        for key,expr in {"mass":b["id"]+".body.m","Ixx":b["id"]+".body.I_11","Iyy":b["id"]+".body.I_22","Izz":b["id"]+".body.I_33","Ixy":b["id"]+".body.I_21","Ixz":b["id"]+".body.I_31","Iyz":b["id"]+".body.I_32"}.items():
            row["columns"][key]=_alias(lines,columns,"body."+b["id"]+"."+key,expr)
        body_contract.append(row)
    for j in actual_joints:
        jid=j["id"];child=j["child"];carrier=j["kind"]=="active_revolute";q0=(qF0 if child.startswith("F") else qR0) if carrier else -(qF0 if child.startswith("F") else qR0)
        lines.extend(["    MB.Parts.FixedTranslation mount_"+child+"(r="+arr([0,0,0] if module and carrier else j["mount"])+",animation=false);",
          "    MB.Joints.Revolute "+jid+"(n={0,-1,0},useAxisFlange=true,animation=false,phi(start="+num(q0)+",fixed=true),w(start=0,fixed=true),stateSelect=StateSelect.always);",
          "    "+("D" if smooth_drive and carrier else "C")+".FiniteDrive "+j["drive_id"]+"(gear="+("100" if carrier else "25")+",continuous="+("100" if carrier else "20")+",peak="+("150" if carrier else "40")+",maxSpeed="+("0.25" if carrier else "4")+",maxPower="+("1e6" if carrier else "250")+");"])
        jc=dict(j,columns={})
        for key,expr in {"q":jid+".phi","w":jid+".w","a":jid+".a","tau":j["drive_id"]+".tau","qref":"qRefF" if child=="F_carrier" else "qRefR" if child=="R_carrier" else jid+".phi","wref":"wRefF" if child=="F_carrier" else "wRefR" if child=="R_carrier" else "wTarget_"+child,"error":jid+".phi-qRefF" if child=="F_carrier" else jid+".phi-qRefR" if child=="R_carrier" else "0","aref":"der(wRefF)" if child=="F_carrier" else "der(wRefR)" if child=="R_carrier" else "der(wTarget_"+child+")","saturated":j["drive_id"]+".clipped"}.items():
            jc["columns"][key]=_alias(lines,columns,"joint."+jid+"."+key,expr,"Boolean" if key=="saturated" else "Real")
        for end in ("parent","child"):
            frame=jid+(".frame_a" if end=="parent" else ".frame_b")
            force_var="force_"+jid+"_"+end
            torque_var="torque_"+jid+"_"+end
            lines.append("    Real "+force_var+"[3]=Frames.resolve1("+frame+".R,"+frame+".f);")
            lines.append("    Real "+torque_var+"[3]=Frames.resolve1("+frame+".R,"+frame+".t);")
            for key,expr in {"Fx":"-"+force_var+"[1]","Fz":"-"+force_var+"[3]","Ty":torque_var+"[2]"}.items():
                jc["columns"][end+key]=_alias(lines,columns,"joint."+jid+"."+end+key,expr)
            target=next(x for x in body_contract if x["id"]==j[end])
            target["loads"].append(dict(id=jid+"_"+end,point_local=[0,0,0] if end=="child" else ([0,0,0] if module and carrier else j["mount"]),columns={k:jc["columns"][end+k] for k in ("Fx","Fz","Ty")},axis="math-minus-y",force_frame="world"))
        joint_contract.append(jc)
        dc=dict(id=j["drive_id"],body_id=child,joint_id=jid,multiplicity=2,physical_ids=next(b for b in bodies if b["id"]==child)["physical_ids"],gear=100 if carrier else 25,eta=.8,kt=.1,ke=.1,Ra=.2,driver_eta=.95,continuous=100 if carrier else 20,peak=150 if carrier else 40,peak_duration=1,max_speed=.25 if carrier else 4,max_power=1e6 if carrier else 250,columns={},kind="carrier" if carrier else "wheel")
        for key,field in {"tau":"tau","wrel":"wrel","power":"power","current":"current","voltage":"voltage","gear_loss":"gearLoss","copper_loss":"copperLoss","driver_loss":"driverLoss","dc":"dc","peak_elapsed":"peakElapsed","copper_energy":"copperEnergy","gear_energy":"gearEnergy","driver_energy":"driverEnergy"}.items():dc["columns"][key]=_alias(lines,columns,"drive."+dc["id"]+"."+key,dc["id"]+"."+field)
        if smooth_drive and carrier:
            for key in ('command','baseTorque','localTorque','speedWeight','clipped'):
                dc['columns'][key]=_alias(lines,columns,'drive.'+dc['id']+'.'+key,dc['id']+'.'+key,'Boolean' if key=='clipped' else 'Real')
        drive_contract.append(dc)
    for b in actual_bodies:
        if b["kind"]!="wheel_pair":continue
        wid=b["id"];par=b["parent"];cp=physics["proposed_contact_parameters"]
        lines.extend(["    C.FeatureContact contact_"+wid+"(nPoints="+str(points)+",xPoints="+arr([p[0] for p in pts])+",zPoints="+arr([p[1] for p in pts])+",vertexKind="+arr(kinds)+",R="+num(r)+",k="+num(2*cp["per_physical_tyre_k"])+",c="+num(2*cp["c"])+",mu="+num(cp["mu"])+",vreg="+num(cp["vreg"])+");",
          "    MB.Forces.WorldTorque rolling_"+wid+"(resolveInFrame=MB.Types.ResolveInFrameB.world,animation=false);",
          "    Real rr_"+wid+"=-"+num(cp["rolling_coefficient"]*r)+"*contact_"+wid+".normal*tanh("+wid+".omega/"+num(cp["rolling_omega_regularization"])+");",
          "    Real wTarget_"+wid+";","    Real refWheelX_"+wid+";","    Real refWheelZ_"+wid+";","    Real loadedTread_"+wid+";","    Real rollError_"+wid+";","    Real ff_"+wid+";"])
        wc=dict(id=wid,parent=par,r=r,multiplicity=2,physical_ids=b["physical_ids"],columns={})
        for key,expr in {"Fx":"contact_"+wid+".Fx","Fz":"contact_"+wid+".Fz","normal":"contact_"+wid+".normal","penetration":"contact_"+wid+".penetration","slip":"contact_"+wid+".slip","omega_world_y":"-"+wid+".omega","tau_rr":"rr_"+wid,"rolling_loss":"-rr_"+wid+"*"+wid+".omega","rolling_error":"rollError_"+wid,"target_speed":"wTarget_"+wid,"elastic":"contact_"+wid+".elasticEnergy"}.items():wc["columns"][key]=_alias(lines,columns,"wheel."+wid+"."+key,expr)
        body=next(x for x in body_contract if x["id"]==wid)
        ty=_alias(lines,columns,"wheel."+wid+".contactTy",num(r)+"*(sum(contact_"+wid+".faceFt)+sum(contact_"+wid+".vertexFt))+rr_"+wid)
        body["contact_load"]=dict(id="terrain_"+wid,point_local=[0,0,0],columns=dict(Fx=wc["columns"]["Fx"],Fz=wc["columns"]["Fz"],Ty=ty),axis="math-minus-y",force_frame="world")
        for index in range(points-1):
            a,bp=pts[index],pts[index+1];dx,dz=bp[0]-a[0],bp[1]-a[1];length=math.hypot(dx,dz)
            feature=dict(id=wid+"_face_"+str(index+1),owner_id=wid,kind="face",owner_index=["F_A","F_B","R_A","R_B"].index(wid)+1,index=index+1,point=a,end=bp,normal=[-dz/length,dx/length],multiplicity=2,columns={})
            for key,field in {"Fn":"faceFn","Ft":"faceFt","active":"faceEnabled","penetration":"faceDelta","slip":"faceSlip"}.items():feature["columns"][key]=_alias(lines,columns,"feature."+feature["id"]+"."+key,"contact_"+wid+"."+field+"["+str(index+1)+"]","Boolean" if key=="active" else "Real")
            features.append(feature)
        for index in range(1,points-1):
            if kinds[index]!=1:continue
            feature=dict(id=wid+"_vertex_"+str(index+1),owner_id=wid,kind="vertex",owner_index=["F_A","F_B","R_A","R_B"].index(wid)+1,index=index+1,point=pts[index],multiplicity=2,columns={})
            for key,field in {"Fn":"vertexFn","Ft":"vertexFt","active":"vertexEnabled","penetration":"vertexDelta","slip":"vertexSlip","nx":"vertexNormalX","nz":"vertexNormalZ"}.items():feature["columns"][key]=_alias(lines,columns,"feature."+feature["id"]+"."+key,"contact_"+wid+"."+field+"["+str(index+1)+"]","Boolean" if key=="active" else "Real")
            features.append(feature)
        for key in ("x","z","vx","vz"):
            wc["columns"][key]=next(x for x in body_contract if x["id"]==wid)["columns"][key]
        wc["columns"]["rolling_torque_world_y"]=_alias(lines,columns,"wheel."+wid+".rolling_torque_world_y","-rr_"+wid)
        wheel_contract.append(wc)
    lines.extend(["    Real busScale;","    Real requestedPower;","    Real batteryPower;","    Real dumpPower;","    Real mechanicalEnergy;","    Real elasticEnergy;","    Real normalLoss;","    Real frictionLoss;","    Real rollingLoss;","    Real shaftPower;","    Real batteryEnergy(start=2764800,fixed=true);","    Real dumpEnergy(start=0,fixed=true); Real dumpPeakElapsed(start=0,fixed=true);","    Real shaftEnergy(start=0,fixed=true);","    Real contactLossEnergy(start=0,fixed=true);","    Real lossEnergy(start=0,fixed=true);","    Real phaseClock(start=0,fixed=true);","    Real referenceClock;","    Real ref[10];","    Real betaF; Real betaR;","    Real qRefF(start="+num(qF0)+",fixed=true); Real qRefR(start="+num(qR0)+",fixed=true);","    Real wRefF(start=0,fixed=true); Real wRefR(start=0,fixed=true);","    Real wholeExitMinX; Real mainCOMvx; Real totalCOMvx; discrete Boolean exitCrossed(start=false,fixed=true); discrete Real exitReferenceX(start=0,fixed=true); discrete Real completionTime(start=-1,fixed=true); Boolean oldSupportOK;", "    Real targetQF; Real targetQR; Real biasF(start=0,fixed=true); Real biasR(start=0,fixed=true);","    Real phaseU; Real supportXF; Real supportXR;","    Real rollingSpeedF; Real rollingSpeedR;","    Real trackingWait(start=0,fixed=true); Real guardWait(start=0,fixed=true);","    Real eventWait; discrete Real pausedSince(start=-1,fixed=true);","    discrete Integer segment(start=1,fixed=true);","    discrete Real loadedSince(start=-1,fixed=true);","    Boolean loadOK; Boolean guardReady; Boolean trackingOK; Boolean saturated; Boolean paused; Boolean failed; Boolean taskDone;","    Real targetForce; Real targetForce2; Real targetTread; Integer targetOwner; Real targetTread2; Integer targetOwner2; Integer supportF; Integer supportR;","    Real ffCarrierF; Real ffCarrierR;"])
    nseg=max(1,len(plan))
    if sampled:
        supervisor_declarations, supervisor_algorithm, supervisor_aliases = _sampled_supervisor(nseg,down,stable_handoff if n>=3 else None,shared_reference if n>=3 else None)
        lines.extend(supervisor_declarations)
        for key,(expr,typ) in supervisor_aliases.items():_alias(lines,columns,'supervisor.'+key,expr,typ)
    if plan:
        for key,vals in {"edge":[p["end"] for p in plan],"begin":[p["start"] for p in plan],"duration":[p["duration"] for p in plan],"u0":[p["u0"] for p in plan],"u1":[p["u1"] for p in plan],"q0F":[p["qF"] for p in plan],"q0R":[p["qR"] for p in plan],"codes":[p["code"] for p in plan],"sf":[p["sF"] for p in plan],"sr":[p["sR"] for p in plan],"cHeld":[p["c"] for p in plan],"guardOwner":[["F_A","F_B","R_A","R_B"].index(p["targets"][0]["wheel"])+1 if p["targets"] else 0 for p in plan],"guardTread":[p["targets"][0]["tread"] if p["targets"] else 0 for p in plan],"guardOwner2":[["F_A","F_B","R_A","R_B"].index(p["targets"][1]["wheel"])+1 if len(p["targets"])>1 else 0 for p in plan],"guardTread2":[p["targets"][1]["tread"] if len(p["targets"])>1 else 0 for p in plan]}.items():
            integer=key in ("codes","sf","sr","guardOwner","guardTread","guardOwner2","guardTread2")
            lines.append("    parameter "+("Integer" if integer else "Real")+" "+key+"["+str(nseg)+"]="+arr(vals)+";")
        lines.extend(["    parameter Real pf["+str(nseg)+",2]="+arr([p["pF"] for p in plan])+";","    parameter Real pr["+str(nseg)+",2]="+arr([p["pR"] for p in plan])+";"])
    for key,expr,typ in [("mechanical_energy","mechanicalEnergy","Real"),("elastic_energy","elasticEnergy","Real"),("ground_loss_power","normalLoss+frictionLoss+rollingLoss","Real"),("failure_code","if failed then 1 else 0","Integer"),("brake_active","time>=25 and time<=26","Boolean"),("normal_loss","normalLoss","Real"),("friction_loss","frictionLoss","Real"),("rolling_loss","rollingLoss","Real"),("shaft_power","shaftPower","Real"),("shaft_energy","shaftEnergy","Real"),("contact_loss_energy","contactLossEnergy","Real"),("loss_energy","lossEnergy","Real"),("battery_power","batteryPower","Real"),("battery_energy","batteryEnergy","Real"),("dump_power","dumpPower","Real"),("dump_energy","dumpEnergy","Real"),("dump_peak_elapsed","dumpPeakElapsed","Real"),("bus_current","batteryPower/48","Real"),("bus_scale","busScale","Real"),("task_done","taskDone","Boolean"),("phase_clock","phaseClock","Real"),("phase_u","phaseU","Real"),("segment","segment","Integer"),("guard_wait","guardWait","Real"),("tracking_wait","trackingWait","Real"),("event_wait","eventWait","Real"),("load_ok","loadOK","Boolean"),("guard_ready","guardReady","Boolean"),("tracking_ok","trackingOK","Boolean"),("paused","paused","Boolean"),("failed","failed","Boolean"),("target_owner","targetOwner","Integer"),("target_owner2","targetOwner2","Integer"),("target_tread2","targetTread2","Real"),("target_tread","targetTread","Real"),("support_F","supportF","Integer"),("support_R","supportR","Integer"),("reference_front_x","ref[1]","Real"),("reference_front_z","ref[2]","Real"),("reference_rear_x","ref[3]","Real"),("reference_rear_z","ref[4]","Real"),("beta_F","betaF","Real"),("beta_R","betaR","Real"),("support_x_F","supportXF","Real"),("support_x_R","supportXR","Real")]:_alias(lines,columns,key,expr,typ)
    if module:
        lines.append("    Real fixtureForce[3]=Frames.resolve1(planar.frame_b.R,planar.frame_b.f);")
        lines.append("    Real fixtureTorque[3]=Frames.resolve1(planar.frame_b.R,planar.frame_b.t);")
        fixtureColumns={"Fx":_alias(lines,columns,"fixture.Fx","-fixtureForce[1]"),"Fz":_alias(lines,columns,"fixture.Fz","-fixtureForce[3]"),"Ty":_alias(lines,columns,"fixture.Ty","fixtureTorque[2]")}
        next(b for b in body_contract if b["id"]=="chassis")["loads"].append(dict(id="fixture_pitch",point_local=[0,0,0],columns=fixtureColumns,axis="math-minus-y",force_frame="world"))
        _alias(lines,columns,"fixture.power","fixtureTorque[2]*planar.revolute.w")
    hardware=_hardware(design,[b["id"] for b in actual_bodies])
    for key,expr,typ in [("completion_time","completionTime","Real"),("exit_reference_x","exitReferenceX","Real"),("exit_crossed","exitCrossed","Boolean"),("vehicle_trailing_x","wholeExitMinX","Real"),("main_com_vx","mainCOMvx","Real"),("total_com_vx","totalCOMvx","Real"),("old_support_ok","oldSupportOK","Boolean")]:
        _alias(lines,columns,key,expr,typ)
    _alias(lines,columns,"touch_bias_F","biasF")
    _alias(lines,columns,"touch_bias_R","biasR")
    if shared_reference is not None and n>=3:
        lines.extend(['    Modelica.Blocks.Continuous.LimIntegrator sharedRate(k=1,outMax=1,outMin=0,y_start=0,use_reset=true);', '    Real sharedPhaseRate; Real sharedRemaining; Boolean sharedTerminalCruise;'])
        _alias(lines,columns,'shared_reference.rate','sharedPhaseRate')
        _alias(lines,columns,'shared_reference.free_rate','sharedRate.y')
        _alias(lines,columns,'shared_reference.remaining','sharedRemaining')
    lines.append("  equation")
    lines.append("    connect(world.frame_b,planar.frame_a); connect(planar.frame_b,chassis.frame);")
    if module:lines.append("    connect(planar.pitchAxis,fixturePitch.flange);")
    for j in actual_joints:
        child=j["child"];jid=j["id"];did=j["drive_id"]
        lines.extend(["    connect("+j["parent"]+".frame,mount_"+child+".frame_a);","    connect(mount_"+child+".frame_b,"+jid+".frame_a);","    connect("+jid+".frame_b,"+child+".frame);","    connect("+did+".shaft,"+jid+".axis); connect("+did+".support,"+jid+".support);","    "+did+".busScale=busScale;"])
    for w in wheel_contract:
        wid=w["id"];par=w["parent"]
        lines.extend(["    connect("+wid+".frame,contact_"+wid+".frame); contact_"+wid+".omegaAbsolute=-"+wid+".omega;","    connect("+wid+".frame,rolling_"+wid+".frame_b); rolling_"+wid+".torque={0,-rr_"+wid+",0};"])
    driveids=[d["id"] for d in drive_contract]
    def summ(xs):return "+".join("("+x+")" for x in xs) or "0"
    lines.extend(["    requestedPower="+summ(["max(0,"+d+".requestedDC)" for d in driveids])+";","    busScale=min(1,(48*80-80)/max(1,requestedPower));","    batteryPower=80+"+summ(["max(0,"+d+".dc)" for d in driveids])+";","    dumpPower="+summ(["max(0,-"+d+".dc)" for d in driveids])+";","    shaftPower="+summ([d+".power" for d in driveids])+";","    elasticEnergy="+summ(["contact_"+w["id"]+".elasticEnergy" for w in wheel_contract])+";","    normalLoss="+summ(["contact_"+w["id"]+".normalLossPower" for w in wheel_contract])+";","    frictionLoss="+summ(["contact_"+w["id"]+".frictionLossPower" for w in wheel_contract])+";","    rollingLoss="+summ(["-rr_"+w["id"]+"*"+w["id"]+".omega" for w in wheel_contract])+";","    mechanicalEnergy=elasticEnergy+"+summ([b["id"]+".kinetic+"+("0" if probe else b["id"]+".potential") for b in actual_bodies])+";","    der(batteryEnergy)=-batteryPower; der(dumpEnergy)=dumpPower; der(shaftEnergy)=shaftPower; der(dumpPeakElapsed)=if dumpPower>300 then 1 else 0;","    der(contactLossEnergy)=normalLoss+frictionLoss+rollingLoss;","    der(lossEnergy)=normalLoss+frictionLoss+rollingLoss+"+summ([d+".gearLoss+"+d+".copperLoss+"+d+".driverLoss" for d in driveids])+";"])
    trailing=[w["id"]+".x-"+num(r) for w in wheel_contract]
    for shape in hardware:
        bid=shape["body_id"];phi=next(b["columns"]["phi"] for b in body_contract if b["id"]==bid)
        locations=shape.get("vertices",shape.get("endpoints",[shape.get("center",[0,0,0])]))
        for loc in locations:trailing.append(bid+".x+"+scalar_atom(loc[0])+"*cos("+phi+")-"+scalar_atom(loc[2])+"*sin("+phi+")-"+scalar_atom(shape.get("radius",0)))
    lines.append("    wholeExitMinX=min({"+",".join(trailing)+"});")
    lines.append("    mainCOMvx=der(chassis.comx);")
    lines.append("    totalCOMvx=("+summ([num(b["m"])+"*der("+b["id"]+".comx)" for b in body_contract])+")/"+num(sum(b["m"] for b in body_contract))+";")
    oldchecks=[]
    for i,p in enumerate(plan):
        parts=[]
        for target in p.get("targets",[]):
            if target["old_face"]:
                parts.append("contact_"+target["old_wheel"]+".faceFn["+str(target["old_face"])+"]>0")
        if parts:oldchecks.append("(segment<>"+str(i+1)+" or guardReady or ("+" and ".join(parts)+"))")
    lines.append("    oldSupportOK="+(" and ".join(oldchecks) or "true")+";")
    if probe or flat:
        # Retain the exact fixed-duration condition and verifier task_done gate.
        # At terminal(), evaluate time literally instead of the cached/hysteretic
        # event relation at stopTime. No earlier completion or tolerance is added.
        # MLS 3.6 section 3.7.5: terminal() and noEvent().
        lines.extend(["    ref={"+num(x0+B/2)+","+num(z0)+","+num(x0-B/2)+","+num(z0)+",0,0,0,0,0,0};","    betaF=0; betaR=0; phaseU=0; supportF=1; supportR=1; targetOwner=0; targetTread=0; targetOwner2=0; targetTread2=0; targetForce=0; targetForce2=0;","    referenceClock=phaseClock; supportXF=0; supportXR=0; rollingSpeedF=0; rollingSpeedR=0;","    loadOK=true; guardReady=true; trackingOK=true;","    taskDone=if terminal() then noEvent(time>="+str(stop)+") else time>="+str(stop)+";"])
    else:
        lines.extend(["    referenceClock="+(num(elapsed)+"-phaseClock" if down else "phaseClock")+";",
          "    phaseU=if codes[segment]==9 then C.cruiseDistance(max(0,referenceClock-begin[segment])) elseif codes[segment]==0 and "+("true" if down else "false")+" then u1[segment]-C.cruiseDistance(max(0,edge[segment]-referenceClock)) else u0[segment]+(u1[segment]-u0[segment])*C.smooth5((referenceClock-begin[segment])/duration[segment]);",
          "    ref=referenceGeometryInline(phaseU,codes[segment],q0F[segment],q0R[segment],pf[segment,:],pr[segment,:],sf[segment],sr[segment],"+",".join(num(x) for x in (H,D,rho,B,r,0 if module else zc,theta,n*H if not module else 2*H))+",cHeld[segment],"+num(design["path"]["correction"]["DeltaCritical"])+","+("true" if down else "false")+","+num(reflect)+");",
          "    betaF=ref[5]; betaR=ref[6]; supportXF=ref[7]; supportXR=ref[8];",
          "    rollingSpeedF=der(supportXF); rollingSpeedR=der(supportXR);",
          "    supportF="+("sr[segment]" if down else "sf[segment]")+"; supportR="+("sf[segment]" if down else "sr[segment]")+";",
          "    targetOwner=guardOwner[segment]; targetTread=guardTread[segment]; targetOwner2=guardOwner2[segment]; targetTread2=guardTread2[segment];"])
        ownerloads=[]
        for oi,wid in enumerate(["F_A","F_B","R_A","R_B"],1):
            if not any(w["id"]==wid for w in wheel_contract):continue
            terms=["(if abs("+num(f["point"][1])+"-targetTread*"+num(H)+")<1e-8 then contact_"+wid+".faceFn["+str(f["index"])+"] else 0)" for f in features if f["owner_id"]==wid and f["kind"]=="face" and f["normal"][1]>.9]
            ownerloads.append("(if targetOwner=="+str(oi)+" then "+summ(terms)+" else 0)")
        lines.extend(["    targetForce="+summ(ownerloads)+"; targetForce2="+summ([x.replace("targetOwner","targetOwner2").replace("targetTread","targetTread2") for x in ownerloads])+";","    loadOK=(targetOwner==0 or targetForce>=40) and (targetOwner2==0 or targetForce2>=40);","    guardReady=targetOwner==0 or (loadOK and loadedSince>=0 and time-loadedSince>=0.2);",
          "    trackingOK=abs(qRefF-F_carrier_joint.phi)<=0.01"+(" and abs(qRefR-R_carrier_joint.phi)<=0.01" if not module else "")+" and "+(" and ".join("abs(rollError_"+w["id"]+")<=0.001" for w in wheel_contract))+";",
          "    taskDone="+("phaseClock>="+num(elapsed)+" and guardReady and trackingOK and not failed" if module else "segment=="+str(1 if down else nseg)+" and exitCrossed and chassis.x-exitReferenceX>=0.5 and chassis.vx>=0.08 and mainCOMvx>=0.08 and totalCOMvx>=0.08 and guardReady and trackingOK and not failed")+";"])
    if probe:
        lines.append("    der(biasF)=0; der(biasR)=0;")
        lines.extend(["    targetQF=0; targetQR=0; der(qRefF)=0; der(qRefR)=0; der(wRefF)=0; der(wRefR)=0;","    ffCarrierF=0; ffCarrierR=0;"])
    elif flat:
        lines.append("    der(biasF)=0; der(biasR)=0;")
        lines.extend(["    targetQF=-planar.phi; targetQR=-planar.phi;","    der(qRefF)=wRefF; der(qRefR)=wRefR;","    der(wRefF)=max(-0.04,min(0.04,4*(max(-0.25,min(0.25,2*(targetQF-qRefF)))-wRefF)));","    der(wRefR)=max(-0.04,min(0.04,4*(max(-0.25,min(0.25,2*(targetQR-qRefR)))-wRefR)));"])
    else:
        endpoint="referenceClock<=begin[segment]" if down else "referenceClock>=edge[segment]"
        biasSign=1 if down else -1
        for group,ownerSet in (("F","(((targetOwner==1 or targetOwner==2) and targetForce<40) or ((targetOwner2==1 or targetOwner2==2) and targetForce2<40))"),("R","(((targetOwner==3 or targetOwner==4) and targetForce<40) or ((targetOwner2==3 or targetOwner2==4) and targetForce2<40))")):
            lines.append("    der(bias"+group+")=max(-0.01,min(0.01,5*((if "+endpoint+" and "+ownerSet+" then "+num(biasSign*math.pi/360)+" else 0)-bias"+group+")));")
        lines.extend(["    targetQF=betaF-planar.phi+biasF; targetQR=betaR-planar.phi+biasR;","    der(qRefF)=wRefF; der(qRefR)=wRefR;","    der(wRefF)=max(-0.04,min(0.04,4*(max(-0.25,min(0.25,der(betaF)-planar.revolute.w+2*(targetQF-qRefF)))-wRefF)));","    der(wRefR)=max(-0.04,min(0.04,4*(max(-0.25,min(0.25,der(betaR)-planar.revolute.w+2*(targetQR-qRefR)))-wRefR)));"])
    if not probe:
        for group in ("F","R"):
            if module and group=="R":lines.append("    ffCarrierR=0;");continue
            parts=[]
            for w in wheel_contract:
                if w["id"].startswith(group):
                    wid=w["id"];parts.append("-("+wid+".x-"+group+"_carrier.x)*contact_"+wid+".Fz+("+wid+".z-"+group+"_carrier.z)*contact_"+wid+".Fx-rr_"+wid)
            inertia=next(b["inertia"][1][1] for b in bodies if b["id"]==group+"_carrier")+2*3.5*rho*rho
            lines.append("    ffCarrier"+group+"="+summ(parts)+"+"+num(inertia)+"*der(wRef"+group+");")
    for j in actual_joints:
        cid=j["child"];did=j["drive_id"]
        if j["kind"]=="active_revolute":
            group=cid[0]
            cmd="(if time<1 then 0 elseif time<9 then 0.01*sin(Modelica.Constants.pi*(time-1)/4) else 0)" if probe and group=="F" else "0" if probe else "ffCarrier"+group+"+600*(qRef"+group+"-"+j["id"]+".phi)+80*(wRef"+group+"-"+j["id"]+".w)"
            lines.append("    "+did+".command="+cmd+";")
    for w in wheel_contract:
        wid=w["id"];group=wid[0];lab=1 if wid.endswith("A") else 2
        if probe:target="0";rollerr="0"
        elif flat:target="-((if time<"+num(settle)+" then 0 elseif time<25 then 0.1 elseif time<26 then max(0,0.1-0.1*(time-25)) else 0)/"+num(r)+")-"+group+"_carrier.omega";rollerr="0"
        else:target="-(if loadedTread_"+wid+">=40 then der(refWheelX_"+wid+")/"+num(r)+" else 0)-"+group+"_carrier.omega";rollerr="if loadedTread_"+wid+">=40 then "+wid+".x-refWheelX_"+wid+" else 0"
        if correction is not None and n>=3:
            speed="der(refWheelX_"+wid+")+noEvent(max(-"+num(correction['speed_cap_m_s'])+",min("+num(correction['speed_cap_m_s'])+",-"+num(correction['gain_s_inv'])+"*rollError_"+wid+")))"
            target="-(if loadedTread_"+wid+">=40 then ("+speed+")/"+num(r)+" else 0)-"+group+"_carrier.omega"
            if stable_handoff is not None:
                speed="der(refWheelX_"+wid+")+noEvent(max(-"+num(correction['speed_cap_m_s'])+",min("+num(correction['speed_cap_m_s'])+",-"+num(correction['gain_s_inv'])+"*("+wid+".x-refWheelX_"+wid+"))))"
                weight="C.smooth5(noEvent(max(0,min(1,loadedTread_"+wid+"/40))))"
                target="-("+weight+"*("+speed+"))/"+num(r)+"-"+group+"_carrier.omega"
        refx="ref[1]" if group=="F" else "ref[3]"
        refz="ref[2]" if group=="F" else "ref[4]"
        sign=1 if lab==1 else -1
        lines.append("    refWheelX_"+wid+"="+refx+"+"+scalar_atom(sign*rho)+"*cos(beta"+group+");")
        lines.append("    refWheelZ_"+wid+"="+refz+"+"+scalar_atom(sign*rho)+"*sin(beta"+group+");")
        horizontal=["contact_"+wid+".faceFn["+str(f["index"])+"]" for f in features if f["owner_id"]==wid and f["kind"]=="face" and f["normal"][1]>.9]
        lines.append("    loadedTread_"+wid+"="+summ(horizontal)+";")
        lines.extend(["    wTarget_"+wid+"="+target+";","    rollError_"+wid+"="+rollerr+";","    ff_"+wid+"=-rr_"+wid+";","    "+wid+"_drive.command="+("0" if probe else "ff_"+wid+"+20*(wTarget_"+wid+"-"+wid+"_joint.w)")+";"])
    event_limit="if not loadOK then 2 else 5"
    if stable_handoff is not None and n>=3:
        endpoint="referenceClock<=begin[segment]" if down else "referenceClock>=edge[segment]"
        event_limit="if not oldSupportOK then 2 elseif "+endpoint+" and not guardReady and trackingOK and not saturated then 40 else 5"
    lines.extend(["    saturated="+("false" if probe else " or ".join(d+".clipped" for d in driveids))+";",
      "    failed=guardWait>40 or trackingWait>20 or eventWait>("+event_limit+")"+("" if plan else "")+" or batteryEnergy<0 or dumpEnergy>5000 or batteryPower>48*80+1e-6 or dumpPower>1000+1e-6 or dumpPeakElapsed>1;",
      "    "+("pauseRaw" if sampled else "paused")+"=(not trackingOK or not oldSupportOK or saturated or failed) or ("+("phaseClock>="+num(elapsed) if not plan else ("referenceClock<=begin[segment]" if down else "referenceClock>=edge[segment]"))+" and not guardReady);",
      "    der(phaseClock)=if time<1 or paused then 0 else if time<8 then C.smooth5((time-1)/7) else 1;",
      "    der(trackingWait)=if time>=1 and (not trackingOK or saturated) then 1 else 0;",
      "    der(guardWait)=if time>=1 and not loadOK and "+("referenceClock<=begin[segment]" if down else "referenceClock>=edge[segment]" if plan else "false")+" then 1 else 0;",
      "    eventWait=if paused and pausedSince>=0 then time-pausedSince else 0;"])
    if sampled:
        lines.extend(supervisor_algorithm)
    elif plan:
        # Each owned state is one atomic algorithm equation (MLS 3.6 11.1.2).
        # Keep the exit flag/reference paired; all event statements stay exact.
        lines.extend(["  algorithm","    when initial() then","      segment:="+str(nseg if down else 1)+";","    elsewhen "+("referenceClock<=begin[pre(segment)]" if down else "referenceClock>=edge[pre(segment)]")+" and guardReady and not failed then","      segment:="+("max(1,pre(segment)-1)" if down else "min("+str(nseg)+",pre(segment)+1)")+";","    end when;","  algorithm","    when {initial(),change(segment),change(loadOK)} then loadedSince:=if loadOK then time else -1; end when;"])
    else:
        lines.extend(["  algorithm","    when initial() then segment:=1; loadedSince:=0; end when;"])
    if plan:lines.append("  algorithm")
    lines.append("    when {initial(),change(paused)} then pausedSince:=if paused then time else -1; end when;")
    if plan:lines.append("  algorithm")
    lines.append("    when initial() then exitCrossed:=false; exitReferenceX:=0; elsewhen wholeExitMinX>="+num((n-1)*D+.02 if n else 100)+" then exitCrossed:=true; exitReferenceX:=chassis.x; end when;")
    if plan:lines.append("  algorithm")
    lines.append("    when initial() then completionTime:=-1; elsewhen taskDone then completionTime:=time;"+(" terminate(\"ACTUAL_COMPLETE_CRUISING\");" if plan and not module else "")+" end when;")
    lines.append("    annotation(experiment(StartTime=0,StopTime="+str(stop)+",Tolerance=1e-6,Interval=0.02));")
    lines.append("  end "+class_name+";")
    # Explicit phase-linked transfer metadata, not a predicted-time PASS.
    for i,p in enumerate(plan):
        for target in p["targets"]:
            wid,tread=target["wheel"],target["tread"]
            transfers.append(dict(id="transfer_"+str(i+1)+"_"+wid,segment=i+1,target_wheel=wid,old_wheel=target["old_wheel"],target_feature=wid+"_face_"+str(target["face"]),old_features=[target["old_wheel"]+"_face_"+str(target["old_face"])] if target["old_face"] else [],target_tread=tread,target_height=tread*H,threshold_pair_N=40,hysteresis_s=.2,phase_column=columns["segment"],owner_column=columns["target_owner"],guard_ready_column=columns["guard_ready"]))
    metadata=dict(name=name,numerical_columns=dict(position_midpoint=columns["body.chassis.x"],energy_mechanical=columns["mechanical_energy"],energy_battery=columns["battery_energy"],energy_dump=columns["dump_energy"]),event_columns=[columns["segment"],columns["exit_crossed"],columns["task_done"]],chassis_id="chassis",gravity=0 if probe else 9.81,front_wheel_ids=[w["id"] for w in wheel_contract if w["id"].startswith("F")],rear_wheel_ids=[w["id"] for w in wheel_contract if w["id"].startswith("R")],direction="down" if down else "up" if n else "flat",exit_edge_x=(n-1)*D if n else None,model_class="V11GeneratedActive."+class_name,stop_time=stop,scope="active_joint_free_moving_support_probe" if probe else "one_twin_module_pitch_constrained_half_vehicle_load_fixture" if module else "full_vehicle",body_ids=[b["id"] for b in actual_bodies],drive_ids=driveids,terrain=dict(points=pts,vertex_kind=kinds),vertex_kind=kinds,steps=pn,forward_down=down,front_wheels=[w["id"] for w in wheel_contract if w["id"].startswith("F")],settle_before_s=1,brake_trigger_s=25 if flat else None,exit_boundary=(n-1)*D if n else None,reference_segments=plan,nominal_reference_s=elapsed,initial_position=[x0,z0],initial_carrier_angles=[qF0,qR0],reflection=dict(x=True,time=True,front_rear_ids=True) if down else None,task_done_column=columns["task_done"])
    text="\n".join(lines)
    if shared_reference is not None and n>=3:
        # One virtual progress drives every geometric reference. No actual pose is assigned.
        for group,q0 in (('F',qF0),('R',qR0)):
            text=text.replace('Real qRef'+group+'(start='+num(q0)+',fixed=true);','Real qRef'+group+';')
            text=text.replace('Real wRef'+group+'(start=0,fixed=true);','Real wRef'+group+';')
        endpoint_raw=num(elapsed)+'-phaseClock' if down else 'phaseClock'
        text=text.replace('    referenceClock='+endpoint_raw+';', '    referenceClock=if sharedTerminalCruise then '+endpoint_raw+' else noEvent(max(begin[segment],min(edge[segment],'+endpoint_raw+')));')
        text=text.replace('    targetQF=betaF-planar.phi+biasF; targetQR=betaR-planar.phi+biasR;', '    targetQF=betaF-planar.phi; targetQR=betaR-planar.phi;')
        text=text.replace('    der(qRefF)=wRefF; der(qRefR)=wRefR;', '    qRefF=targetQF; qRefR=targetQR; wRefF=der(betaF)-planar.revolute.w; wRefR=der(betaR)-planar.revolute.w;')
        for group in ('F','R'):
            prefix='    der(wRef'+group+')=max(-0.04'
            text='\n'.join(row for row in text.splitlines() if not row.startswith(prefix))
            text='\n'.join('    der(bias'+group+')=0;' if row.startswith('    der(bias'+group+')=max') else row for row in text.splitlines())
        terminal='segment==1' if down else 'segment=='+str(nseg)
        remaining='('+num(elapsed)+'-begin[segment])-phaseClock' if down else 'edge[segment]-phaseClock'
        progress=['    sharedTerminalCruise='+terminal+';', '    sharedRemaining=noEvent(max(0,'+remaining+'));', '    sharedRate.reset=segment<>pre(segment);', '    sharedRate.u=if time<1 or paused then -0.5 else 0.5;', '    sharedPhaseRate=if sharedTerminalCruise then noEvent(max(0,min(1,sharedRate.y))) else noEvent(min(max(0,min(1,sharedRate.y)),sharedStopRate(sharedRemaining)));', '    der(phaseClock)=sharedPhaseRate;']
        if down and shared_reference.get('slow_descent') is True:
            progress[4]='    sharedPhaseRate=if sharedTerminalCruise then noEvent(max(0,min(1,sharedRate.y))) else noEvent(min(0.5*max(0,min(1,sharedRate.y)),sqrt(0.5)*sharedStopRate(sharedRemaining)));'
        text=text.replace('    der(phaseClock)=if time<1 or paused then 0 else if time<8 then C.smooth5((time-1)/7) else 1;', '\n'.join(progress))
        metadata['shared_reference']=dict(authorization=shared_reference,max_progress_rate=1,progress_acceleration_cap=.5,relative_acceleration_includes_actual_body=True,terminal_cruise_not_endpoint_limited=True,endpoint_rate_threshold=1e-10,physical_pose_assignment=False)
        if down and shared_reference.get('slow_descent') is True:
            metadata['shared_reference'].update(nonterminal_max_progress_rate=.5,nonterminal_progress_acceleration_cap=.25,terminal_progress_rate=1,terminal_progress_acceleration_cap=.5)
    if sampled:
        text=text.replace('and not failed;', 'and not failed and not failureSeen;')
        text=text.replace('o_failure_code=if failed then 1 else 0;', 'o_failure_code=if failed or failureSeen then 1 else 0;')
        metadata['sampled_supervisor']=dict(period_s=.02,initial_segment=nseg if down else 1,stage_count=nseg,authorization=controller,columns={key:columns['supervisor.'+key] for key in supervisor_aliases})
    return text,dict(**metadata,hardware=hardware,definitions=dict(power="tau_math_minus_y*wrel_math_minus_y"),columns=columns,bodies=body_contract,joints=joint_contract,drives=drive_contract,wheels=wheel_contract,features=features,transfers=transfers,metadata=metadata)


def _reference_scalar_expansion(text):
    """Expand C3.referenceGeometry's assignments; no new physical law or function call.

    Original C3 source remains read-only. Scalar equations retain the same Real
    if/max/min relations, equality choices and default GenerateEvents=false
    function semantics through noEvent on every expanded RHS; permit der().
    """
    import re
    match=re.search(r"^    ref=referenceGeometryInline\((.*)\);$",text,re.M)
    if not match:return text
    args=[];start=depth=0
    for i,ch in enumerate(match[1]):
        if ch in "([":depth+=1
        elif ch in ")]":depth-=1
        elif ch=="," and depth==0:args.append(match[1][start:i]);start=i+1
    args.append(match[1][start:])
    if len(args)!=20:raise ValueError("REFERENCE_SIGNATURE_DRIFT")
    u,stage,qf0,qr0,pf,pr,sf,sr,H,D,rho,B,r,zc,theta,top,held,critical,down,reflect=args
    def pivot(value,index):
        if not re.fullmatch(r"\w+\[segment,:\]",value):raise ValueError("REFERENCE_PIVOT_DRIFT")
        return value.replace(":",str(index))
    prefix="referenceScalar_"
    v=lambda n:prefix+n
    rows=[]
    def put(name,expr):rows.append((v(name),expr));return v(name)
    qf=put("qF",f"if {stage}==1 or {stage}==2 or {stage}==3 or {stage}==4 or {stage}==5 then {qf0}-{u} else {qf0}")
    qr=put("qR",f"if {stage}==4 or {stage}==5 or {stage}==6 or {stage}==7 or {stage}==8 then {qr0}-{u} else {qr0}")
    sgf=put("sgF",f"if {sf}==1 then -1 else 1");sgr=put("sgR",f"if {sr}==1 then -1 else 1")
    f0x=put("f0x",f"{pivot(pf,1)}+{sgf}*{rho}*cos({qf})")
    f0z=put("f0z",f"{pivot(pf,2)}+{sgf}*{rho}*sin({qf})")
    r0x=put("r0x",f"{pivot(pr,1)}+{sgr}*{rho}*cos({qr})")
    r0z=put("r0z",f"{pivot(pr,2)}+{sgr}*{rho}*sin({qr})")
    fz=put("fz",f"if {stage}>=6 then {top}+{r} else {f0z}")
    rz=put("rz",f"if {stage}<=3 then {r} else {r0z}")
    fx0=put("fx0",f"if {stage}>=6 then {r0x}+sqrt(max(1e-12,{B}^2-({fz}-{r0z})^2)) else {f0x}")
    rx0=put("rx0",f"if {stage}<=3 then {f0x}-sqrt(max(1e-12,{B}^2-({f0z}-{r})^2)) else {r0x}")
    delta=put("delta",f"{fz}-{rz}");alpha=put("alpha",f"asin(max(-1,min(1,{delta}/{B})))")
    au=put("au",f"if {stage}==4 or {stage}==6 then {u}+{theta} else {u}")
    a=put("a",f"if {stage}==0 or {stage}==9 then 0 else max(0,{H}/2*sin({au})-{D}/2*(1-cos({au})))")
    b=put("b",f"if {stage}==0 or {stage}==9 then 0 else {zc}*sin({alpha})")
    fx=put("fx",f"sqrt(max(1e-12,{B}^2-{delta}^2))/2+{zc}*{delta}/{B}")
    base=lambda z:f"sqrt({B}^2-({z})^2)/2+{zc}*({z})/{B}"
    c1=put("c1",f"({base(critical)})-({base('1.5*'+H)})")
    c2=put("c2",f"({base('1.5*'+H)})-({base('0.5*'+H)})")
    cc=put("cc",f"if {stage}==0 then {u} elseif {stage}==9 then {held}+{u} elseif {stage}==6 then (if {delta}<{critical} then ({base(critical)})-{fx} else 0) elseif {stage}==7 then (if {u}<=2*{theta} then {c1}+({base('1.5*'+H)})-{fx} else {c1}+{c2}) elseif {stage}==8 then {c1}+{c2}+({base('0.5*'+H)})-{fx} else {held}")
    shift=put("shift",f"{a}+{b}+{cc}")
    fx1=put("fx1",f"{fx0}+{a}+{b}+{cc}");rx1=put("rx1",f"{rx0}+{a}+{b}+{cc}")
    sxF=put("supportF",f"{fx1}-{sgf}*{rho}*cos({qf})");sxR=put("supportR",f"{rx1}-{sgr}*{rho}*cos({qr})")
    up=[fx1,fz,rx1,rz,qf,qr,sxF,sxR,shift,alpha]
    dn=[f"{reflect}-{rx1}",rz,f"{reflect}-{fx1}",fz,f"Modelica.Constants.pi-{qr}",f"Modelica.Constants.pi-{qf}",f"{reflect}-{sxR}",f"{reflect}-{sxF}",shift,f"-{alpha}"]
    eq=[f"    {name}=noEvent({expr});" for name,expr in rows]
    eq += [f"    ref[{i}]=noEvent(if {down} then {dd} else {uu});" for i,(uu,dd) in enumerate(zip(up,dn),1)]
    declaration="\n".join(f"    Real {name};" for name,_ in rows)
    text=text[:match.start()]+"\n".join(eq)+text[match.end():]
    if text.count("\n  equation")!=1:raise ValueError("REFERENCE_EQUATION_SECTION_DRIFT")
    return text.replace("\n  equation","\n"+declaration+"\n  equation",1)

def lower(design, controller=None, correction=None, stable_handoff=None, shared_reference=None):
    if controller is not None:
        if controller.get('schema')!='v11-approved-sampled-supervisor-1' or controller.get('period_s')!=.02 or controller.get('accepted') is not True or len(controller.get('authorization_sha256',''))!=64:
            raise ValueError('UNAPPROVED_CONTROLLER_ADDENDUM')
    if correction is not None:
        if correction.get('schema')!='v11-bounded-position-correction-1' or correction.get('accepted') is not True or correction.get('gain_s_inv')!=2.0 or correction.get('speed_cap_m_s')!=.02 or len(correction.get('authorization_sha256',''))!=64:
            raise ValueError('UNAPPROVED_POSITION_CORRECTION')
    if stable_handoff is not None:
        if stable_handoff.get('schema')!='v11-stable-handoff-1' or stable_handoff.get('accepted') is not True or len(stable_handoff.get('authorization_sha256',''))!=64 or correction is None:
            raise ValueError('UNAPPROVED_STABLE_HANDOFF')
    if shared_reference is not None:
        if shared_reference.get('schema')!='v11-shared-reference-1' or shared_reference.get('accepted') is not True or len(shared_reference.get('authorization_sha256',''))!=64 or stable_handoff is None or controller is None:
            raise ValueError('UNAPPROVED_SHARED_REFERENCE')
    scenes={};sources=["within;\npackage V11GeneratedActive\n  \"Source-bound active twin wheel tree; UNVERIFIED until independent checks\""]
    if shared_reference is not None:
        sources.append('''  function sharedStopRateDerivative
    input Real remaining;
    input Real der_remaining;
    output Real der_rate;
  algorithm
    der_rate:=if remaining>0 then 0.5*der_remaining/sqrt(remaining) else 0;
  end sharedStopRateDerivative;
  function sharedStopRate
    input Real remaining;
    output Real rate;
  algorithm
    rate:=if remaining>0 then sqrt(remaining) else 0;
    annotation(derivative=sharedStopRateDerivative);
  end sharedStopRate;''')
    # Compiler-only expansion; inherited function equations and event semantics remain unchanged.
    # referenceGeometry is expanded at each relevant generated model into scalar equations.
    for name,(class_name,stop,n,down) in SCENES.items():
        text,contract=_scene(design,name,class_name,stop,n,down,controller,correction,stable_handoff,shared_reference);sources.append(_reference_scalar_expansion(text));scenes[name]=contract
    sources.append('  annotation(uses(Modelica(version="4.0.0")));\nend V11GeneratedActive;\n')
    output_contract=dict(schema="v11-active-cluster-output-contract-1",hardware_collision_scope="Declared planar terrain plus static y-lane envelopes; intentional bearing/axle/feed/rod-stator interfaces excluded, no 3D dynamic certification",representation=design["representation"],columns={"time":"time"},scenes=scenes,thresholds=design["thresholds"],definitions=dict(body_axis="math-minus-y",joint_axis="MSL-minus-y",drive_torque="aggregate_pair_MSL_minus_y",drive_current="per_physical_motor",drive_power="aggregate_pair_tau*wrel",feature_force="aggregate_pair",normal_metadata="face constants; vertex normals actual CSV",fixture_not_vehicle="Module pitch support reaction is a bench fixture and never a task acceptance",hardware="UNKNOWN_AI_PROXY",fail_safe_brake="UNKNOWN_NOT_MODELLED"),variable_filter="^(time|o_.*)$")
    return "\n\n".join(sources),output_contract
