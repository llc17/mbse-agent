model ZLTECH_Full_Mission_Asym2
  "Standalone planar three-equivalent-wheel-group stair mission; diagnostic model"

  // Tail2 geometry-only derivation; vehicle, contact forces and control inherited unchanged.
  // Source: week13/6轮上下楼仿真/运动/6lun_zhenggeliucheng.mo.
  // This is a new diagnostic copy, not a validated hardware model.
  // The source actually sets stepH=0.25 although its comment says 15 cm.
  // The default demo uses 0.05 m; capability must be checked for this 239 mm model.
  // Earlier 254 mm results are historical and are not reused as 239 mm results.
  // This is not a maximum-height claim. Default torques are the rated group limits.
  // Torque mapping revision: two driven wheels per longitudinal group.
  // Main motor: rated 20, peak 60 N.m EACH; nose: rated 8.5, peak 25 EACH.
  // Group rated limits below are 40/17 N.m. Group peak limits are 120/50.
  // Peak duration is UNKNOWN: a separate peak run is an upper-limit diagnostic,
  // not a continuous-duty or verified-hardware capability claim.
  // Main diameter is corrected to the user-selected drawing value, 239 mm.
  // Main reference group inertia follows 0.5 * (2 * 9 kg) * R_main^2; not measured.
  parameter Real m=85.0;
  parameter Real g=9.81;
  parameter Real I_pitch=20.0 "Whole-vehicle pitch inertia with wheels locked";
  parameter Real R_main=0.1195 "239 mm outside diameter selected by user from drawing";
  parameter Real R_nose=0.07;
  parameter Real T_main_max=40.0 "Group rated: 2 x 20 N.m; peak group = 120";
  parameter Real T_nose_max=17.0 "Group rated: 2 x 8.5 N.m; peak group = 50";
  parameter Real a=0.22;
  parameter Real b=0.23;
  parameter Real L_nose_ext=0.32;
  parameter Real L_nose_CoM=a+L_nose_ext;
  parameter Real h_nose=0.19;
  parameter Real wheel_J[3]={0.02205,0.12852225,0.12852225}
    "Reference solid-disc values only, NOT measured wheel inertias; nose/front/rear";
  parameter Real R[3]={R_nose,R_main,R_main};
  parameter Real Tmax[3]={T_nose_max,T_main_max,T_main_max};
  parameter Real bodyX[3]={L_nose_CoM,a,-b};
  parameter Real bodyZ[3]={h_nose-R_main,0,0};
  parameter Real stepH=0.05;
  parameter Real stepD=0.28;
  parameter Real h19=0.05 "Up riser19; mirrored down riser2";
  parameter Real h20=0.05 "Up riser20; mirrored down riser1";
  parameter Real d19=0.28 "x19-x18; mirrored down interval2";
  parameter Real d20=0.28 "x20-x19; mirrored down interval1";
  parameter Integer n_steps=20;
  final parameter Integer nEndSteps=2 "Frozen number of short top risers";
  parameter Real fixedTopTread=0.28 "Frozen legacy final tread allocation";
  parameter Real x_start_up=2.0;
  parameter Real x_last_up=x_start_up+(n_steps-nEndSteps-1)*stepD+d19+d20;
  parameter Real x_end_up=x_last_up+fixedTopTread;
  parameter Real len_flat=2.0;
  parameter Real x_start_down=x_end_up+len_flat;
  parameter Real x_last_down=x_start_down+d20+d19+(n_steps-nEndSteps-1)*stepD;
  parameter Real x_end_down=x_last_down+fixedTopTread;
  parameter Real H_peak=(n_steps-nEndSteps)*stepH+h19+h20;
  parameter Real k_ground=4.0e4;
  parameter Real c_ground=3.0e3;
  parameter Real mu_s=0.85;
  parameter Real v_eps=0.005 "Smooth Coulomb small-slip approximation, m/s";
  parameter Real target_speed=0.4 "Desired wheel peripheral speed; approximately body speed on flat ground";
  parameter Real speed_error_scale=0.1;
  parameter Real driveScale=1 "Set zero for unpowered diagnostics";
  parameter Real x0=0;
  parameter Real z0=R_main+0.01;
  parameter Real psi0=0;
  parameter Real v0=0;
  parameter Real vz0=0;
  parameter Real omega0=0;
  parameter Real u0[3]={0,0,0};
  parameter Real centerTolerance=1e-7 "Center-inside-solid detection tolerance only";
  final parameter Integer nReach=integer(ceil(max(R)/min(stepD,min(d19,d20))));
  final parameter Integer nLocal=2*nReach+1;
  final parameter Integer nContact=3+3*nLocal;

  Real x(start=x0,fixed=true);
  Real z(start=z0,fixed=true);
  Real psi(start=psi0,fixed=true) "Counterclockwise body pitch";
  Real v(start=v0,fixed=true) "World horizontal velocity";
  Real vz(start=vz0,fixed=true) "World vertical velocity";
  Real omega_psi(start=omega0,fixed=true);
  Real u[3](start=u0,each fixed=true)
    "Clockwise wheel spin relative to body; absolute clockwise spin is u-omega_psi";
  Real T[3];
  Real power_factor "Body-speed diagnostic only; actual motor commands are per wheel";
  Real wheel_command[3],rim_speed[3];
  Real cx[3],cz[3];
  Real x_n,z_n,x_f,z_f,x_r,z_r;
  Real valid[3,nContact] "1 for a geometrically eligible exterior feature, else 0";
  Real geometry_n[6,nContact],geometry_f[6,nContact],geometry_r[6,nContact];
  Real qx[3,nContact],qz[3,nContact] "Terrain closest point";
  Real nx[3,nContact],nz[3,nContact] "Outward terrain normal";
  Real px[3,nContact],pz[3,nContact] "Wheel circle force point = center-R*normal";
  Real gap[3,nContact] "Signed exterior feature distance minus wheel radius";
  Real delta[3,nContact];
  Real vn[3,nContact];
  Real vslip[3,nContact];
  Real Fn[3,nContact],Ft[3,nContact];
  Real Fx_contact[3,nContact],Fz_contact[3,nContact];
  Real M_contact[3,nContact];
  Real PnormalLoss[3,nContact];
  Real PfrictionLoss[3,nContact];
  Real Fx_wheel[3],Fz_wheel[3];
  Real Fx_total,Fz_total,Mcontact_total;
  Real terrainHeightAtWheel[3];
  Real insideDepth[3];
  Real maxCompression;
  Real Ekin,Epot,Ucontact,Etotal;
  Real Pdrive,PnormalDiss,PfrictionDiss,PbodyDiss,Pdiss;
  Real Wdrive(start=0,fixed=true),Wdiss(start=0,fixed=true);
  Real Eref(start=0,fixed=false);
  Real energyResidual;

  function localFeatures
    "Finite line interiors plus convex vertices; no concave-corner point spring"
    input Real wx;
    input Real wz;
    input Real radius;
    input Real startUp;
    input Real startDown;
    input Real height;
    input Real depth;
    input Real height19;
    input Real height20;
    input Real depth19;
    input Real depth20;
    input Integer count;
    input Integer endCount;
    input Integer reach;
    output Real features[6,3+3*(2*reach+1)] "Rows qx/qz/nx/nz/gap/valid";
  protected
    Real qx[3+3*(2*reach+1)];
    Real qz[3+3*(2*reach+1)];
    Real nx[3+3*(2*reach+1)];
    Real nz[3+3*(2*reach+1)];
    Real gap[3+3*(2*reach+1)];
    Real valid[3+3*(2*reach+1)];
    Boolean up;
    Integer j0,j,k;
    Real lastUp,lastDown,peak,edge,nextEdge,lower,upper,dist,dx,dz;
  algorithm
    lastUp:=startUp+(count-endCount-1)*depth+depth19+depth20;
    lastDown:=startDown+depth20+depth19+(count-endCount-1)*depth;
    peak:=(count-endCount)*height+height19+height20;
    // The three global horizontal faces join the local treads exactly once.
    for s in 1:3+3*(2*reach+1) loop
      qx[s]:=wx;
      qz[s]:=0;
      nx[s]:=0;
      nz[s]:=1;
      gap[s]:=1;
      valid[s]:=0;
    end for;
    qz[1]:=0;
    gap[1]:=wz-radius;
    valid[1]:=if wx<startUp and wz>=0 then 1 else 0;
    qz[2]:=peak;
    gap[2]:=wz-peak-radius;
    valid[2]:=if wx>lastUp and wx<startDown and wz>=peak then 1 else 0;
    qz[3]:=0;
    gap[3]:=wz-radius;
    valid[3]:=if wx>lastDown and wz>=0 then 1 else 0;
    // Only one staircase can be in reach: the separation assertion below
    // makes this switch occur where every local staircase force is zero.
    up:=wx<(lastUp+startDown)/2;
    // Local windows follow the two short top intervals and mirrored descent.
    j0:=if up then
      (if wx<startUp+17*depth then integer(floor((wx-startUp)/depth))+1
       else if wx<startUp+17*depth+depth19 then 18
       else if wx<lastUp then 19 else 20)
      else (if wx<startDown+depth20 then 1
            else if wx<startDown+depth20+depth19 then 2
            else integer(floor((wx-startDown-depth20-depth19)/depth))+3);
    j0:=max(1,min(count,j0));
    for l in 1:2*reach+1 loop
      j:=j0+l-reach-1;
      k:=3+3*(l-1);
      edge:=if up then (if j<=18 then startUp+(j-1)*depth
              else if j==19 then startUp+17*depth+depth19 else lastUp)
            else (if j==1 then startDown else if j==2 then startDown+depth20
              else startDown+depth20+depth19+(j-3)*depth);
      nextEdge:=edge+(if up then (if j==18 then depth19 else if j==19 then depth20 else depth)
                         else (if j==1 then depth20 else if j==2 then depth19 else depth));
      lower:=if up then (if j<=18 then (j-1)*height
                 else if j==19 then 18*height else 18*height+height19)
             else (if j==1 then peak-height20 else if j==2 then 18*height else (count-j)*height);
      upper:=if up then (if j<=18 then j*height
                 else if j==19 then 18*height+height19 else peak)
             else (if j==1 then peak else if j==2 then peak-height20 else (count-j+1)*height);
      // Slot k+1: tread interior. The last tread belongs to a global face.
      qx[k+1]:=wx;
      qz[k+1]:=if up then upper else lower;
      gap[k+1]:=wz-qz[k+1]-radius;
      valid[k+1]:=if j>=1 and j<count and wx>edge and wx<nextEdge
                       and wz>=qz[k+1] then 1 else 0;
      // Slot k+2: riser interior. A center behind the exterior face is invalid.
      qx[k+2]:=edge;
      qz[k+2]:=wz;
      nx[k+2]:=if up then -1 else 1;
      nz[k+2]:=0;
      dist:=nx[k+2]*(wx-edge);
      gap[k+2]:=dist-radius;
      valid[k+2]:=if j>=1 and j<=count and wz>lower and wz<upper
                       and dist>=0 then 1 else 0;
      // Slot k+3: convex top corner, restricted to its outward normal sector.
      qx[k+3]:=edge;
      qz[k+3]:=upper;
      dx:=wx-edge;
      dz:=wz-upper;
      dist:=sqrt(dx*dx+dz*dz);
      nx[k+3]:=if dist>1e-12 then dx/max(dist,1e-12) else (if up then -1 else 1);
      nz[k+3]:=if dist>1e-12 then dz/max(dist,1e-12) else 0;
      gap[k+3]:=dist-radius;
      valid[k+3]:=if j>=1 and j<=count and dz>=0
                       and (if up then dx<=0 else dx>=0) then 1 else 0;
    end for;
    features:={qx,qz,nx,nz,gap,valid};
  end localFeatures;

  function solidHeight
    "Independent center-inside-solid guard; this height never generates a force"
    input Real wx;
    input Real startUp;
    input Real startDown;
    input Real height;
    input Real depth;
    input Real height19;
    input Real height20;
    input Real depth19;
    input Real depth20;
    input Integer count;
    input Integer endCount;
    output Real h;
  algorithm
    h:=if wx<startUp then 0
       else if wx<startUp+17*depth then (integer(floor((wx-startUp)/depth))+1)*height
       else if wx<startUp+17*depth+depth19 then 18*height
       else if wx<startUp+17*depth+depth19+depth20 then 18*height+height19
       else if wx<startDown then 18*height+height19+height20
       else if wx<startDown+depth20 then 18*height+height19
       else if wx<startDown+depth20+depth19 then 18*height
       else if wx<startDown+depth20+depth19+17*depth then
         (17-integer(floor((wx-startDown-depth20-depth19)/depth)))*height
       else 0;
  end solidHeight;

initial equation
  Eref=Etotal;
  assert(m>0 and g>0 and R_main>0 and R_nose>0,"Positive mass, gravity and radii required");
  assert(I_pitch>sum(wheel_J) and min(wheel_J)>0,
    "I_pitch is locked-wheel total inertia; residual body inertia must be positive");
  assert(stepH>0 and stepD>0 and h19>0 and h20>0 and d19>0 and d20>0 and n_steps==20 and nEndSteps==2 and len_flat==2 and fixedTopTread==0.28,"Invalid staircase parameters");
  assert(fixedTopTread+len_flat>2*max(R),"Top span must separate the two local feature windows");
  assert(k_ground>0 and c_ground>=0 and mu_s>=0 and v_eps>0 and speed_error_scale>0,
    "Invalid contact/control parameters");

equation
  der(x)=v;
  der(z)=vz;
  der(psi)=omega_psi;
  power_factor=noEvent(driveScale*max(-1,min(1,(target_speed-v)/speed_error_scale)));
  // One complete matrix result per call avoids the OMC 1.25.7 multi-output
  // array-slice C-template failure observed in compile_probe_02.
  geometry_n=localFeatures(cx[1],cz[1],R[1],x_start_up,x_start_down,stepH,stepD,h19,h20,d19,d20,n_steps,nEndSteps,nReach);
  geometry_f=localFeatures(cx[2],cz[2],R[2],x_start_up,x_start_down,stepH,stepD,h19,h20,d19,d20,n_steps,nEndSteps,nReach);
  geometry_r=localFeatures(cx[3],cz[3],R[3],x_start_up,x_start_down,stepH,stepD,h19,h20,d19,d20,n_steps,nEndSteps,nReach);
  for j in 1:nContact loop
    qx[1,j]=geometry_n[1,j]; qx[2,j]=geometry_f[1,j]; qx[3,j]=geometry_r[1,j];
    qz[1,j]=geometry_n[2,j]; qz[2,j]=geometry_f[2,j]; qz[3,j]=geometry_r[2,j];
    nx[1,j]=geometry_n[3,j]; nx[2,j]=geometry_f[3,j]; nx[3,j]=geometry_r[3,j];
    nz[1,j]=geometry_n[4,j]; nz[2,j]=geometry_f[4,j]; nz[3,j]=geometry_r[4,j];
    gap[1,j]=geometry_n[5,j]; gap[2,j]=geometry_f[5,j]; gap[3,j]=geometry_r[5,j];
    valid[1,j]=geometry_n[6,j]; valid[2,j]=geometry_f[6,j]; valid[3,j]=geometry_r[6,j];
  end for;
  for i in 1:3 loop
    cx[i]=x+bodyX[i]*cos(psi)-bodyZ[i]*sin(psi);
    cz[i]=z+bodyX[i]*sin(psi)+bodyZ[i]*cos(psi);
    // Relative-spin feedback avoids unlimited energy in an airborne wheel.
    // Absolute clockwise wheel spin is u-omega_psi. No motor-speed rating is invented.
    rim_speed[i]=R[i]*(u[i]-omega_psi);
    wheel_command[i]=noEvent(driveScale*max(-1,min(1,(target_speed-rim_speed[i])/speed_error_scale)));
    T[i]=Tmax[i]*wheel_command[i];
    terrainHeightAtWheel[i]=noEvent(solidHeight(cx[i],x_start_up,x_start_down,stepH,stepD,h19,h20,d19,d20,n_steps,nEndSteps));
    insideDepth[i]=noEvent(max(0,terrainHeightAtWheel[i]-cz[i]));
    assert(noEvent(insideDepth[i]<=centerTolerance),
      "Wheel center entered terrain solid: stop instead of lifting a tunneled wheel");
    for j in 1:nContact loop
      px[i,j]=cx[i]-R[i]*nx[i,j];
      pz[i,j]=cz[i]-R[i]*nz[i,j];
      delta[i,j]=noEvent(if valid[i,j]>0.5 then max(0,-gap[i,j]) else 0);
      // Normal speed includes center motion and rigid-body rotation.
      vn[i,j]=(v-omega_psi*(pz[i,j]-z))*nx[i,j]
               +(vz+omega_psi*(px[i,j]-x))*nz[i,j];
      // t=(nz,-nx); the same circle point and radius enter force and spin work.
      vslip[i,j]=(v-omega_psi*(pz[i,j]-z))*nz[i,j]
                 -(vz+omega_psi*(px[i,j]-x))*nx[i,j]-R[i]*u[i];
      Fn[i,j]=noEvent(if valid[i,j]>0.5 and gap[i,j]<0 then
                       max(0,k_ground*delta[i,j]-c_ground*vn[i,j]) else 0);
      Ft[i,j]=-mu_s*Fn[i,j]*tanh(vslip[i,j]/v_eps);
      Fx_contact[i,j]=Fn[i,j]*nx[i,j]+Ft[i,j]*nz[i,j];
      Fz_contact[i,j]=Fn[i,j]*nz[i,j]-Ft[i,j]*nx[i,j];
      M_contact[i,j]=(px[i,j]-x)*Fz_contact[i,j]-(pz[i,j]-z)*Fx_contact[i,j];
      // This also accounts for clipped spring/damper force during separation.
      PnormalLoss[i,j]=(k_ground*delta[i,j]-Fn[i,j])*vn[i,j];
      PfrictionLoss[i,j]=-Ft[i,j]*vslip[i,j];
    end for;
    Fx_wheel[i]=sum(Fx_contact[i,:]);
    Fz_wheel[i]=sum(Fz_contact[i,:]);
    wheel_J[i]*(der(u[i])-der(omega_psi))=T[i]-R[i]*sum(Ft[i,:]);
  end for;
  x_n=cx[1]; z_n=cz[1];
  x_f=cx[2]; z_f=cz[2];
  x_r=cx[3]; z_r=cz[3];
  Fx_total=sum(Fx_wheel);
  Fz_total=sum(Fz_wheel);
  Mcontact_total=sum(M_contact);
  m*der(v)=Fx_total-50*v;
  m*der(vz)=Fz_total-m*g;
  // Wheel/body motor reactions are internal: do not subtract motor torque again.
  I_pitch*der(omega_psi)-sum(wheel_J[i]*der(u[i]) for i in 1:3)
    =Mcontact_total-300*omega_psi;
  maxCompression=max(delta);
  Ekin=0.5*m*(v*v+vz*vz)+0.5*(I_pitch-sum(wheel_J))*omega_psi*omega_psi
        +0.5*sum(wheel_J[i]*(u[i]-omega_psi)^2 for i in 1:3);
  Epot=m*g*z;
  Ucontact=0.5*k_ground*sum(delta.*delta);
  Etotal=Ekin+Epot+Ucontact;
  Pdrive=sum(T.*u);
  PnormalDiss=sum(PnormalLoss);
  PfrictionDiss=sum(PfrictionLoss);
  PbodyDiss=50*v*v+300*omega_psi*omega_psi;
  Pdiss=PnormalDiss+PfrictionDiss+PbodyDiss;
  der(Wdrive)=Pdrive;
  der(Wdiss)=Pdiss;
  der(Eref)=0;
  energyResidual=Etotal-Eref-Wdrive+Wdiss;
  annotation(experiment(StartTime=0,StopTime=300,Tolerance=1e-5,Interval=0.01),
    __OpenModelica_simulationFlags(s="dassl",maxStepSize="0.002"),
    Documentation(info="<html><p>New diagnostic copy of the supplied six-wheel example. All three equivalent groups use the same finite staircase faces and convex corners. No force is generated by a floor-height function. Smooth Coulomb friction is an explicit small-slip approximation. Reference wheel inertias are not measurements. Formal task/physical validity requires the separate checker and simulation evidence.</p></html>"));
end ZLTECH_Full_Mission_Asym2;
