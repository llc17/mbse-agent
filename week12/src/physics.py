"""Generic planar rigid axle vehicle; penalty contacts on actual stair faces.

All wheel pairs aggregate two tires. Body translation and pitch are solved,
and each axle has an absolute clockwise wheel angular speed. No path is imposed.
This development model is not calibrated to hardware.
"""
from __future__ import annotations

import math
from copy import deepcopy


def default_designs(stair: dict, wheel_counts: list[int], *, payload_kg: float = 20.0) -> list[dict]:
    """Return development designs with the explicit V11 payload in total mass.

    ``payload_kg`` is an input to the single generation path.  It defaults to
    the V11 requirement so an omitted legacy argument cannot silently restore
    the withdrawn 2 kg sample.
    """
    if payload_kg < 0:
        raise ValueError("payload_kg must be non-negative")
    h = stair["riser_m"]
    # The starting case is a 150 mm riser.  Wheel radius is sized above the
    # riser while keeping wheel diameter below the wheelbase, so the generated
    # four-wheel layout has no geometric wheel overlap.
    routes = [
        ("compact", min(wheel_counts), max(0.12, 1.4*h), 0.42, 120.0, 5.0),
        ("clearance", min(wheel_counts), max(0.19, 1.6*h), 0.62, 180.0, 9.0),
        ("endurance", max(wheel_counts), max(0.17, 1.5*h), 0.78, 320.0, 8.0),
    ]
    result = []
    for name, wheels, radius, base, wh, torque in routes:
        masses = {"chassis": 5.5 + 3*base, "wheels": wheels*(0.35+2*radius),
                  "motors_and_gears": wheels*0.85, "battery": wh/160,
                  "electronics": 0.6, "payload": float(payload_kg)}
        mass = sum(masses.values())
        result.append({
            "candidate_id": name, "wheel_count": wheels, "wheel_radius_m": radius,
            "wheelbase_m": base, "track_m": 0.40, "tire_width_m": 0.06,
            "cg_above_axle_m": 0.055, "body_half_height_m": 0.045,
            "body_length_m": base+0.10, "mass_breakdown_kg": masses, "mass_kg": mass,
            "pitch_inertia_kg_m2": mass*(base**2+0.16**2)/12,
            "wheel_pair_inertia_kg_m2": 2*0.5*(0.35+2*radius)*radius**2,
            "wheel_peak_torque_nm": torque, "wheel_continuous_torque_nm": torque*0.6,
            "wheel_no_load_speed_rad_s": 7.0, "gear_ratio": 30.0,
            "motor_efficiency": 0.80, "gear_efficiency": 0.88, "controller_efficiency": 0.95,
            "motor_idle_w_per_wheel": 0.8, "motor_copper_w_at_continuous": 4.0,
            "battery_wh": wh, "battery_voltage_v": 24.0, "battery_max_current_a": 25.0,
            "usable_energy_fraction": 0.80, "auxiliary_w": 8.0,
            "rationale": {"compact": "Small envelope and low mass; least step/torque margin",
                          "clearance": "Larger wheels and stronger drives; greater envelope and energy cost",
                          "endurance": "Larger battery and longer support base; heavier with rigid six-wheel contact tradeoffs"}[name],
        })
    return result


def model_name(scenario: str) -> str:
    scenario = {"stair": "ascent"}.get(scenario, scenario)
    if scenario not in ("ascent", "descent", "flat"):
        raise ValueError("scenario must be ascent, descent, or flat")
    return {"ascent": "V11Ascent", "descent": "V11Descent", "flat": "V11Flat"}[scenario]


def generate_model(spec: dict, scenario: str) -> str:
    """Generate a distinct ascent, descent, or level-ground model.

    The descent branch has its own downward step geometry and brake command;
    it is not an inverted ascent trace.  ``stair`` remains an ascent alias for
    callers from the S1 development path.
    """
    car, stair, assumptions = spec["design"], spec["stair"], spec["assumptions"]
    scenario = {"stair": "ascent"}.get(scenario, scenario)
    name = model_name(scenario)
    mode = {"flat": 0, "ascent": 1, "descent": 2}[scenario]
    n = max(1, car["wheel_count"] // 2)
    count = stair["count"] if mode else 0
    offsets = [0.0] if n == 1 else [-car["wheelbase_m"] / 2 + i * car["wheelbase_m"] / (n - 1) for i in range(n)]
    speed = assumptions["stair_speed_m_s"] if mode else assumptions["cruise_speed_m_s"]
    radius = car["wheel_radius_m"]
    platform = 1.0
    initial_floor = stair["count"] * stair["riser_m"] if mode == 2 else 0.0
    # Put both axles 0.10 m inside the declared approach/top platform before
    # the first riser.  Starting with the rear axle exactly at the platform
    # edge made the first contact event sensitive to initialization.
    platform_margin = 0.00
    x0 = -platform + platform_margin + car["wheelbase_m"] / 2
    stop = assumptions["stair_stop_s"] if mode else assumptions["flat_stop_s"]
    return f'''model {name}
  import SI = Modelica.Units.SI;
  parameter Integer n={n}, steps={count}, mode={mode};
  parameter SI.Length riser={stair['riser_m']}, tread={stair['tread_m']};
  parameter SI.Length platform={platform};
  // Keep a finite support buffer beyond the last tread so the vehicle remains
  // supported while exitProgress reaches its stop threshold and settles.
  parameter SI.Length platformSupportBuffer=5.00;
  parameter SI.Force supportForceThreshold=1.0;
  parameter SI.Length platformMargin={platform_margin};
  parameter SI.Length initialGap=0.0001;
  parameter SI.Radius R={radius};
  parameter SI.Length axle[n]={{{', '.join(str(v) for v in offsets)}}};
  parameter SI.Length cg={car['cg_above_axle_m']};
  parameter SI.Mass mass={car['mass_kg']};
  parameter SI.Inertia Ibody={car['pitch_inertia_kg_m2']}, Jwheel={car['wheel_pair_inertia_kg_m2']};
  parameter Real mu={assumptions['friction']}, crr={assumptions['rolling_coefficient']};
  parameter Real k=120000, damping=1200, slipReg=0.02, contactReg=0.0002;
  parameter SI.Force normalCap={4*car['mass_kg']*9.81/n};
  parameter SI.Torque Tpeak={2*car['wheel_peak_torque_nm']}, Tcontinuous={2*car['wheel_continuous_torque_nm']};
  parameter SI.Torque brakeLimit={0.35*2*car['wheel_peak_torque_nm']};
  parameter SI.Time stopAt={stop}, settleWindow=1.0, driveRampTime=2.4;
  parameter SI.Time ascentStartupTime=2.0;
  parameter Real stopProgress=0.98, speedGain=12, slipGain=20, pitchGain=2;
  parameter Real ascentStartupSpeed=0.025, ascentStartupTorqueFraction=0.35, ascentTorqueFraction=0.70;
  parameter Real descentSpeedFactor=1.0, descentBrakeStart=0.80, descentBrakeGain=8;
  parameter Real descentSlipGain=6, descentPositiveTorqueFraction=0.60, descentStallBoostFraction=0.20;
  parameter SI.AngularVelocity wFree={car['wheel_no_load_speed_rad_s']};
  parameter Real eta={car['motor_efficiency']*car['gear_efficiency']*car['controller_efficiency']};
  parameter SI.Velocity target={speed};
  parameter SI.Voltage voltage={car['battery_voltage_v']};
  SI.Position x(start={x0}, fixed=true), z(start={initial_floor+radius+car['cg_above_axle_m']}+initialGap, fixed=true);
  SI.Velocity vx(start=0,fixed=true), vz(start=0,fixed=true);
  SI.Angle pitch(start=0,fixed=true);
  SI.AngularVelocity q(start=0,fixed=true), w[n](each start=0,each fixed=true);
  SI.Position wx[n], wz[n];
  SI.Velocity vwx[n], vwz[n];
  SI.Force fx[n], fz[n];
  SI.Torque torque[n], rolling[n];
  Real contact[n,9];
  Real supportLevel[n];
  Real slipFeedback[n], driveRamp, tractionLimit[n], descentBrakeWindow;
  SI.Power mechanicalPower, batteryPower, copperPower, frictionLoss;
  SI.Current batteryCurrent;
  SI.Energy batteryEnergy(start=0,fixed=true), mechanicalWork(start=0,fixed=true);
  Real overloadTime[n](each start=0,each fixed=true);
  Real kineticEnergy, potentialEnergy, penetration, slipSpeed;
  Real supportCount, frontNormal, rearNormal, entryProgress, exitProgress, settledIndicator, commandSpeed;
  function smoothPositive
    input Real x, eps;
    output Real y;
  algorithm
    y := 0.5*(x + sqrt(x*x + eps*eps));
  end smoothPositive;
  function smoothWindow
    input Real x, lower, upper, eps;
    output Real y;
  algorithm
    y := 0.5*(tanh((x-lower)/eps) + tanh((upper-x)/eps));
  end smoothWindow;
  function lowerGate
    input Real x, upper, eps;
    output Real y;
  algorithm
    y := 0.5*(1 + tanh((upper-x)/eps));
  end lowerGate;
  function upperGate
    input Real x, lower, eps;
    output Real y;
  algorithm
    y := 0.5*(1 + tanh((x-lower)/eps));
  end upperGate;
  function contacts
    input Real px, pz, ux, uz, omega, r, mu, stiffness, damper, reg, height, depth;
    input Integer ns;
    input Integer contactMode;
    input Real platformLength;
    input Real supportBuffer;
    input Real maxNormal;
    output Real c[9] "Fx,Fz,total normal,max penetration,max slip,friction loss,min contact x,max contact x,tangential force";
  protected
    Real left, right, top, edge, gap, nx, nz, pen, vn, vt, normal, friction, gate;
  algorithm
    c := {{0,0,0,0,0,0,1e6,-1e6,0}};
    // Horizontal faces and vertical risers.  Descent faces are approached from
    // the left and therefore have the opposite normal to ascent faces.
    for kind in 1:3 loop
      for j in 0:ns loop
        gate := 0; nx := 0; nz := 1; gap := 1e6;
        if contactMode==0 then
          left := -platformLength; right := platformLength + 10000; top := 0;
          if kind==1 and j==0 then gate := smoothWindow(px,left,right,reg); gap := pz-top; end if;
        elseif contactMode==1 then
          left := if j==0 then -platformLength else (j-1)*depth;
          right := if j==ns then ns*depth+platformLength+supportBuffer else j*depth;
          top := j*height;
          if kind==1 then
            gate := smoothWindow(px,left,right,reg); gap := pz-top;
          elseif kind==2 and j>0 then
            edge := (j-1)*depth;
            gate := smoothWindow(pz,top-height,top,reg)*lowerGate(px,edge+r,reg);
            nx := -1; nz := 0; gap := edge-px;
          end if;
        else
          left := if j==0 then -platformLength else (j-1)*depth;
          right := if j==ns then ns*depth+platformLength+supportBuffer else j*depth;
          top := if j==0 then ns*height else (ns-j)*height;
          if kind==1 then
            gate := smoothWindow(px,left,right,reg); gap := pz-top;
          elseif kind==2 and j>0 then
            edge := (j-1)*depth;
            // The vehicle still travels in +x while descending.  The wheel
            // approaches each riser from the left, so the riser reaction is
            // -x; using the opposite normal accelerates the vehicle into the
            // stair and produces the R1 divergence.
            gate := smoothWindow(pz,top,top+height,reg)*lowerGate(px,edge+r,reg);
            nx := -1; nz := 0; gap := edge-px;
          end if;
        end if;
        pen := gate*smoothPositive(r-gap,reg);
        vn := ux*nx+uz*nz;
        vt := ux*nz-uz*nx-r*omega;
        normal := min(maxNormal, smoothPositive(gate*(stiffness*pen-damper*vn),reg));
        friction := -mu*normal*tanh(vt/reg);
        c[1] := c[1]+normal*nx+friction*nz;
        c[2] := c[2]+normal*nz-friction*nx;
        c[3] := c[3]+normal;
        c[4] := max(c[4],pen);
        c[5] := max(c[5],gate*abs(vt));
        c[6] := c[6]-friction*vt;
        c[9] := c[9]+friction;
        c[7] := min(c[7],gate*(px-r*nx)+(1-gate)*1e6);
        c[8] := max(c[8],gate*(px-r*nx)-(1-gate)*1e6);
      end for;
    end for;
  end contacts;
equation
  der(x)=vx; der(z)=vz; der(pitch)=q;
  for i in 1:n loop
    wx[i]=x+axle[i]*cos(pitch)+cg*sin(pitch);
    wz[i]=z+axle[i]*sin(pitch)-cg*cos(pitch);
    vwx[i]=vx+(-axle[i]*sin(pitch)+cg*cos(pitch))*q;
    vwz[i]=vz+(axle[i]*cos(pitch)+cg*sin(pitch))*q;
    contact[i,:]=contacts(wx[i],wz[i],vwx[i],vwz[i],w[i],R,mu,k,damping,slipReg,riser,tread,steps,mode,platform,platformSupportBuffer,normalCap);
    fx[i]=contact[i,1]; fz[i]=contact[i,2];
    // Keep support as a continuous diagnostic.  A Boolean threshold here
    // caused OpenModelica event chattering during descent without changing
    // the contact force or any physical acceptance limit.
    supportLevel[i]=noEvent(if contact[i,3] > supportForceThreshold then min(1, max(0, contact[i,3])) else 0);
    slipFeedback[i]=vwx[i]-R*w[i];
    tractionLimit[i]=max(0,mu*contact[i,3]*R);
    torque[i]=if mode==2 then
      max(-0.50*Tpeak, min(descentPositiveTorqueFraction*Tpeak,
        driveRamp*speedGain*(commandSpeed/R-w[i]) + descentSlipGain*slipFeedback[i] - pitchGain*q
        - descentBrakeGain*descentBrakeWindow*max(vx,0)
        + (if vx<0.005 and exitProgress<descentBrakeStart then descentStallBoostFraction*Tpeak else 0)))
    else if mode==1 then
      max(-brakeLimit, min(tractionLimit[i], min(Tpeak*max(0,1-max(0,w[i])/wFree),
        driveRamp*speedGain*(commandSpeed/R-w[i]) +
        driveRamp*(if time<ascentStartupTime then ascentStartupTorqueFraction else ascentTorqueFraction)*Tpeak +
        slipGain*slipFeedback[i] - pitchGain*q)))
    else
      max(-brakeLimit, min(Tpeak*max(0,1-max(0,w[i])/wFree),
        driveRamp*speedGain*(commandSpeed/R-w[i]) + slipGain*slipFeedback[i] - pitchGain*q));
    rolling[i]=crr*contact[i,3]*R*tanh(w[i]/0.1);
    // Tangential contact torque about wheel center is -R*Ft (clockwise convention).
    Jwheel*der(w[i])=torque[i]-rolling[i]-R*contact[i,9];
    der(overloadTime[i])=if torque[i]>Tcontinuous then 1 else 0;
  end for;
  mass*der(vx)=sum(fx);
  mass*der(vz)=sum(fz)-mass*Modelica.Constants.g_n;
  Ibody*der(q)=sum((wx[i]-x)*fz[i]-(wz[i]-z)*fx[i]+torque[i] for i in 1:n);
  mechanicalPower=sum(max(0,torque[i]*w[i]) for i in 1:n);
  copperPower={car['wheel_count']*car['motor_idle_w_per_wheel']}+sum({2*car['motor_copper_w_at_continuous']}*(torque[i]/Tcontinuous)^2 for i in 1:n);
  batteryPower=mechanicalPower/eta+copperPower+{car['auxiliary_w']};
  batteryCurrent=batteryPower/voltage;
  der(batteryEnergy)=batteryPower; der(mechanicalWork)=mechanicalPower;
  frictionLoss=sum(contact[:,6]);
  penetration=max(contact[i,4] for i in 1:n); slipSpeed=max(contact[i,5] for i in 1:n);
  supportCount=sum(supportLevel[i] for i in 1:n);
  frontNormal=contact[n,3]; rearNormal=contact[1,3];
  entryProgress=if mode==0 then 1 else min(1,max(0,(x+platform)/(platform+0.5)));
  exitProgress=if mode==0 then 1 else min(1,max(0,(x-steps*tread)/(platform+0.5)));
  descentBrakeWindow=if mode==2 then upperGate(exitProgress,descentBrakeStart,0.03) else 0;
  commandSpeed=if mode==0 then (if time >= stopAt-settleWindow then 0 else target)
    else if mode==1 then (if time<ascentStartupTime then ascentStartupSpeed else target)
    else if exitProgress >= stopProgress then 0 else target*descentSpeedFactor*(1-descentBrakeWindow);
  driveRamp=min(1,max(0,time/driveRampTime));
  settledIndicator=if abs(vx)<0.01 and abs(q)<0.05 and exitProgress>0.95 then 1 else 0;
  kineticEnergy=0.5*mass*(vx^2+vz^2)+0.5*Ibody*q^2+sum(0.5*Jwheel*w[i]^2 for i in 1:n);
  potentialEnergy=mass*Modelica.Constants.g_n*z;
  annotation(experiment(StartTime=0,StopTime={stop},Tolerance=1e-6,Interval=0.01));
end {name};
'''
