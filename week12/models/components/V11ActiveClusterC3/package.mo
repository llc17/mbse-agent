within;
package V11ActiveClusterC3
  "New active twin wheel adapter; no component or task PASS is implied"
  import SI=Modelica.Units.SI;
  import MB=Modelica.Mechanics.MultiBody;
  import Rot=Modelica.Mechanics.Rotational;
  import Frames=Modelica.Mechanics.MultiBody.Frames;

  model Body
    parameter SI.Mass m=1;
    parameter SI.Acceleration g=9.81;
    parameter SI.Position rCM[3]={0,0,0};
    parameter SI.Inertia I[3,3]=diagonal({0.001,0.001,0.001});
    MB.Interfaces.Frame_a frame;
    MB.Parts.Body body(m=m,r_CM=rCM,I_11=I[1,1],I_22=I[2,2],I_33=I[3,3],
      I_21=I[2,1],I_31=I[3,1],I_32=I[3,2],animation=false,enforceStates=false);
    Real comOffset[3]=Frames.resolve1(frame.R,rCM);
    Real comx=frame.r_0[1]+comOffset[1];
    Real comy=frame.r_0[2]+comOffset[2];
    Real comz=frame.r_0[3]+comOffset[3];
    output SI.Position x=frame.r_0[1];
    output SI.Position y=frame.r_0[2];
    output SI.Position z=frame.r_0[3];
    output SI.Velocity vx=der(x);
    output SI.Velocity vy=der(y);
    output SI.Velocity vz=der(z);
    output SI.Acceleration ax=der(vx);
    output SI.Acceleration az=der(vz);
    output SI.AngularVelocity omega=-body.w_a[2] "math minus y";
    output SI.AngularAcceleration alpha=der(omega);
    output SI.Energy kinetic=0.5*m*(der(comx)^2+der(comy)^2+der(comz)^2)+0.5*I[2,2]*omega^2;
    output SI.Energy potential=m*g*comz;
  equation
    connect(frame,body.frame_a);
  end Body;

  model FeatureContact
    "C2 equations inherited unchanged; public feature arrays expose all summed contacts"
    extends V11ContactC2.PolylineWheelContact;
    output SI.Force faceFn[nPoints-1]=fnFace;
    output SI.Force faceFt[nPoints-1]=ftFace;
    output Boolean faceEnabled[nPoints-1]=faceActive;
    output SI.Length faceDelta[nPoints-1]=deltaFace;
    output SI.Velocity faceSlip[nPoints-1]=vtFace;
    output SI.Force vertexFn[nPoints]=fnVertex;
    output SI.Force vertexFt[nPoints]=ftVertex;
    output Boolean vertexEnabled[nPoints]=vertexActive;
    output SI.Length vertexDelta[nPoints]=deltaVertex;
    output SI.Velocity vertexSlip[nPoints]=vtVertex;
    output Real vertexNormalX[nPoints]=vertexNx;
    output Real vertexNormalZ[nPoints]=vertexNz;
  end FeatureContact;

  model FiniteDrive
    "Pair torque source, true moving bearing support, four quadrant gearbox/copper proxy"
    parameter Integer multiplicity=2;
    parameter Real gear=100;
    parameter Real eta=0.8;
    parameter Real kt=0.1;
    parameter Real ke=0.1;
    parameter Real Ra=0.2;
    parameter Real driverEta=0.95;
    parameter SI.Torque continuous=100;
    parameter SI.Torque peak=150;
    parameter SI.Time peakDuration=1;
    parameter SI.AngularVelocity maxSpeed=0.25;
    parameter SI.Power maxPower=1e6;
    parameter SI.Voltage Vbus=48;
    Rot.Interfaces.Flange_b shaft;
    Rot.Interfaces.Support support;
    Modelica.Blocks.Interfaces.RealInput command;
    Modelica.Blocks.Interfaces.RealInput busScale;
    Rot.Sources.Torque source(useSupport=true);
    output SI.AngularVelocity wrel=der(shaft.phi)-der(support.phi);
    output SI.Torque tau;
    output SI.Power power=tau*wrel;
    output SI.Current current;
    output SI.Voltage voltage;
    output SI.Power gearLoss;
    output SI.Power copperLoss;
    output SI.Power driverLoss;
    output SI.Power dc;
    output SI.Power requestedDC;
    output SI.Time peakElapsed(start=0,fixed=true);
    output Boolean clipped;
    output SI.Energy heat(start=0,fixed=true);
    output SI.Energy copperEnergy(start=0,fixed=true);
    output SI.Energy gearEnergy(start=0,fixed=true);
    output SI.Energy driverEnergy(start=0,fixed=true);
  protected
    SI.Torque torqueBound;
    SI.Torque requested;
    SI.Torque localTorque;
    Real branch;
    SI.AngularVelocity motorSpeed;
    SI.Current currentLimit;
    SI.Current commandedCurrent;
    SI.Current localCurrent;
    SI.Power prePower;
    SI.Power preGear;
    SI.Power preCopper;
    SI.Power preMechanical;
  equation
    motorSpeed=gear*wrel;
    torqueBound=if peakElapsed < peakDuration then peak else continuous;
    requested=max(-torqueBound,min(torqueBound,command/multiplicity));
    branch=if requested*wrel >= 0 then 1/eta else eta;
    currentLimit=peak/(gear*eta*kt);
    commandedCurrent=branch*requested/(gear*kt);
    localCurrent=max(-currentLimit,max((-Vbus-ke*motorSpeed)/Ra,
      min(currentLimit,min((Vbus-ke*motorSpeed)/Ra,commandedCurrent))));
    localTorque=if abs(wrel)>maxSpeed then 0 else
      max(-maxPower/max(abs(wrel),1e-6),min(maxPower/max(abs(wrel),1e-6),localCurrent*gear*kt/branch));
    prePower=multiplicity*localTorque*wrel;
    preGear=if prePower>=0 then (1/eta-1)*prePower else (1-eta)*abs(prePower);
    preMechanical=prePower+preGear;
    preCopper=multiplicity*Ra*(branch*localTorque/(gear*kt))^2;
    requestedDC=preMechanical+preCopper+(1/driverEta-1)*(abs(preMechanical)+preCopper);
    tau=multiplicity*localTorque*max(0,min(1,busScale));
    current=(if tau*wrel>=0 then 1/eta else eta)*tau/(multiplicity*gear*kt);
    voltage=Ra*current+ke*motorSpeed;
    gearLoss=if power>=0 then (1/eta-1)*power else (1-eta)*abs(power);
    copperLoss=multiplicity*Ra*current^2;
    driverLoss=(1/driverEta-1)*(abs(power+gearLoss)+copperLoss);
    dc=power+gearLoss+copperLoss+driverLoss;
    clipped=abs(tau-command)>1e-8;
    der(peakElapsed)=if abs(tau)/multiplicity>continuous+1e-8 then 1 else 0;
    der(heat)=gearLoss+copperLoss+driverLoss;
    der(copperEnergy)=copperLoss; der(gearEnergy)=gearLoss; der(driverEnergy)=driverLoss;
    source.tau=tau;
    connect(source.flange,shaft);
    connect(source.support,support);
  end FiniteDrive;

  function smooth5
    input Real s;
    output Real y;
  protected
    Real a;
  algorithm
    a:=max(0,min(1,s));
    y:=10*a^3-15*a^4+6*a^5;
  end smooth5;

  function cruiseDistance
    "Finite ordinary flat-speed reference: seven-second smooth acceleration then continuous .1m/s cruise"
    input Real t;
    output Real x;
  protected
    Real s;
  algorithm
    s:=max(0,min(1,t/7));
    x:=if t<=7 then 0.7*(2.5*s^4-3*s^5+s^6) else 0.1*(t-3.5);
  end cruiseDistance;

  function referenceGeometry
    "Piecewise analytic complete ascent; descent explicitly reflects x, reverses time and exchanges F/R"
    input Real u;
    input Integer stage;
    input Real qF0;
    input Real qR0;
    input Real frontPivot[2];
    input Real rearPivot[2];
    input Integer frontSupport;
    input Integer rearSupport;
    input Real H;
    input Real D;
    input Real rho;
    input Real B;
    input Real r;
    input Real zc;
    input Real theta;
    input Real top;
    input Real cHeld;
    input Real deltaCritical;
    input Boolean down=false;
    input Real reflectX=0;
    output Real y[10] "CFx CFz CRx CRz betaF betaR supportFx supportRx s alpha";
  protected
    Real f[2];
    Real rr[2];
    Real qF;
    Real qR;
    Real delta;
    Real a;
    Real b;
    Real cc;
    Real fx;
    Real fx0;
    Real c1;
    Real c2;
    Real c3;
    Real alpha;
    Real sf;
    Real sr;
    Real sgF;
    Real sgR;
  algorithm
    qF:=qF0;
    qR:=qR0;
    if stage==1 or stage==2 or stage==3 then
      qF:=qF0-u;
    elseif stage==4 or stage==5 then
      qF:=qF0-u;
      qR:=qR0-u;
    elseif stage==6 or stage==7 or stage==8 then
      qR:=qR0-u;
    end if;
    sgF:=if frontSupport==1 then -1 else 1;
    sgR:=if rearSupport==1 then -1 else 1;
    f:={frontPivot[1]+sgF*rho*cos(qF),frontPivot[2]+sgF*rho*sin(qF)};
    rr:={rearPivot[1]+sgR*rho*cos(qR),rearPivot[2]+sgR*rho*sin(qR)};
    if stage<=3 then
      rr[2]:=r;
      rr[1]:=f[1]-sqrt(max(1e-12,B^2-(f[2]-r)^2));
    elseif stage>=6 then
      f[2]:=top+r;
      f[1]:=rr[1]+sqrt(max(1e-12,B^2-(f[2]-rr[2])^2));
    end if;
    delta:=f[2]-rr[2];
    alpha:=asin(max(-1,min(1,delta/B)));
    a:=max(0,H/2*sin(u)-D/2*(1-cos(u)));
    // Remainder phases start at theta in the common pi phase coordinate.
    if stage==4 or stage==6 then
      a:=max(0,H/2*sin(u+theta)-D/2*(1-cos(u+theta)));
    elseif stage==3 or stage==5 or stage==8 then
      a:=max(0,H/2*sin(u)-D/2*(1-cos(u)));
    end if;
    b:=zc*sin(alpha);
    fx:=sqrt(max(1e-12,B^2-delta^2))/2+zc*delta/B;
    c1:=sqrt(B^2-deltaCritical^2)/2+zc*deltaCritical/B-(sqrt(B^2-(1.5*H)^2)/2+zc*1.5*H/B);
    c2:=sqrt(B^2-(1.5*H)^2)/2+zc*1.5*H/B-(sqrt(B^2-(0.5*H)^2)/2+zc*0.5*H/B);
    c3:=sqrt(B^2-(0.5*H)^2)/2+zc*0.5*H/B-B/2;
    cc:=cHeld;
    if stage==6 then
      cc:=if delta<deltaCritical then sqrt(B^2-deltaCritical^2)/2+zc*deltaCritical/B-fx else 0;
    elseif stage==7 then
      cc:=if u<=2*theta then c1+sqrt(B^2-(1.5*H)^2)/2+zc*1.5*H/B-fx else c1+c2;
    elseif stage==8 then
      cc:=c1+c2+sqrt(B^2-(0.5*H)^2)/2+zc*0.5*H/B-fx;
    end if;
    if stage==0 then
      a:=0; b:=0; cc:=u;
    end if;
    if stage==9 then
      // ordinary post-exit roll: u is forward distance, not a rotation
      a:=0; b:=0; cc:=cHeld+u;
    end if;
    f[1]:=f[1]+a+b+cc;
    rr[1]:=rr[1]+a+b+cc;
    sf:=f[1]-sgF*rho*cos(qF);
    sr:=rr[1]-sgR*rho*cos(qR);
    y:={f[1],f[2],rr[1],rr[2],qF,qR,sf,sr,a+b+cc,alpha};
    if down then
      // physical axes reflect as beta -> -beta, and rear maps to front
      y:={reflectX-rr[1],rr[2],reflectX-f[1],f[2],Modelica.Constants.pi-qR,Modelica.Constants.pi-qF,reflectX-sr,reflectX-sf,a+b+cc,-alpha};
    end if;
  end referenceGeometry;
end V11ActiveClusterC3;
