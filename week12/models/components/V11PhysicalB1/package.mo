within;
package V11PhysicalB1
  "V11 S3-B planar physical components and six frozen development fixtures"

  import SI = Modelica.Units.SI;
  import MB = Modelica.Mechanics.MultiBody;
  import Rot = Modelica.Mechanics.Rotational;
  import Frames = Modelica.Mechanics.MultiBody.Frames;

  partial model ScenarioOutputs
    "Common scalar result contract; the independent variable time is emitted by Modelica"
    output SI.Position x;
    output SI.Position z;
    output SI.Velocity vx;
    output SI.Velocity vz;
    output SI.AngularVelocity omega "Absolute wheel roll speed about world +y";
    output SI.Force Fx "Total terrain force in world x";
    output SI.Force Fz "Total terrain force in world z";
    output SI.Force normal "Sum of unilateral normal-force magnitudes";
    output SI.Length penetration "Largest active feature penetration";
    output SI.Velocity slip "Tangential speed of deepest active feature";
    output SI.Power normalLoss "Instantaneous normal damping dissipation";
    output SI.Power frictionLoss "Instantaneous friction dissipation";
    output SI.Energy elasticEnergy;
    output SI.Energy kineticEnergy;
    output SI.Energy potentialEnergy;
    output SI.Power shaftPower;
    output SI.Power dcPower;
    output SI.Power driveLoss "Instantaneous non-regenerative drive dissipation";
    output SI.Energy energyAvailable;
    output SI.Torque driveTorque "Commanded paired roll torque about +y";
    output SI.Torque supportTorque
      "Carrier pitch-constraint reaction, positive about the Planar n=-y axis";
    output Integer contactCount;
    output Integer featureId;
    output SI.Power normalLossPower;
    output SI.Power frictionLossPower;
    output SI.Power driveLossPower;
    output SI.Energy normalDissipatedEnergy;
    output SI.Energy frictionDissipatedEnergy;
    output SI.Energy driveDissipatedEnergy;
  end ScenarioOutputs;

  model RigidBody2D
    "MSL Body thin wrapper for the frozen x-z / pitch-about-minus-y subset"
    parameter SI.Mass m(min=Modelica.Constants.small) = 1;
    parameter SI.Inertia J(min=Modelica.Constants.small) = 0.001
      "Out-of-plane inertia about the component centre of mass";
    parameter Boolean enforceStates = false;
    MB.Interfaces.Frame_a frame;
    MB.Parts.Body body(
      r_CM={0,0,0},
      m=m,
      I_11=J/2,
      I_22=J,
      I_33=J/2,
      I_21=0,
      I_31=0,
      I_32=0,
      enforceStates=enforceStates,
      animation=false);
    output SI.Position x = body.frame_a.r_0[1];
    output SI.Position z = body.frame_a.r_0[3];
    output SI.Velocity vx = body.v_0[1];
    output SI.Velocity vz = body.v_0[3];
    output SI.AngularVelocity pitchRate = -body.w_a[2]
      "Positive for the project's numerical pitch, i.e. rotation about world -y";
    output SI.Energy kineticEnergy =
      0.5*m*(vx*vx + vz*vz) + 0.5*J*pitchRate*pitchRate;
  equation
    connect(frame, body.frame_a);
  end RigidBody2D;

  model PolylineWheelContact
    "Finite directed-polyline wheel contact with half-open faces and explicit corners"
    parameter Integer nPoints(min=2) = 2;
    parameter SI.Position xPoints[nPoints] = {-100,100};
    parameter SI.Position zPoints[nPoints] = {0,0};
    parameter Integer vertexKind[nPoints] = fill(0, nPoints)
      "1 convex radial feature, -1 concave (two faces allowed), 0 ordinary/end";
    parameter SI.Radius R(min=Modelica.Constants.small) = 0.1;
    parameter SI.TranslationalSpringConstant k(min=0) = 100000;
    parameter SI.TranslationalDampingConstant c(min=0) = 200;
    parameter Real mu(min=0) = 0;
    parameter SI.Velocity vreg(min=Modelica.Constants.small) = 0.001;

    MB.Interfaces.Frame_a frame "Wheel centre/body frame";
    Modelica.Blocks.Interfaces.RealInput omegaAbsolute(unit="rad/s")
      "Absolute wheel roll speed about world +y";

    output SI.Force Fx;
    output SI.Force Fz;
    output SI.Force normal;
    output SI.Length penetration;
    output SI.Velocity slip;
    output Integer contactCount;
    output Integer featureId;
    output SI.Energy elasticEnergy;
    output SI.Power normalLoss;
    output SI.Power frictionLoss;
    output SI.Energy normalDissipatedEnergy(start=0, fixed=true);
    output SI.Energy frictionDissipatedEnergy(start=0, fixed=true);
    output SI.Power normalLossPower;
    output SI.Power frictionLossPower;

  protected
    final parameter Integer nSegments = nPoints - 1;
    final parameter SI.Length segmentLength[nSegments] =
      {sqrt((xPoints[i + 1] - xPoints[i])^2 +
            (zPoints[i + 1] - zPoints[i])^2) for i in 1:nSegments};
    final parameter Real tx[nSegments] =
      {(xPoints[i + 1] - xPoints[i])/segmentLength[i] for i in 1:nSegments};
    final parameter Real tz[nSegments] =
      {(zPoints[i + 1] - zPoints[i])/segmentLength[i] for i in 1:nSegments};
    final parameter Real nx[nSegments] = {-tz[i] for i in 1:nSegments};
    final parameter Real nz[nSegments] = { tx[i] for i in 1:nSegments};

    SI.Position xCenter;
    SI.Position zCenter;
    SI.Velocity vxCenter;
    SI.Velocity vzCenter;
    Real projection[nSegments](each unit="m");
    SI.Position distanceFace[nSegments]
      "Signed normal distance to each directed face";
    SI.Length deltaFace[nSegments];
    SI.Velocity vnFace[nSegments];
    SI.Velocity vtFace[nSegments];
    SI.Force feFace[nSegments];
    SI.Force fdFace[nSegments];
    SI.Force fnFace[nSegments];
    SI.Force ftFace[nSegments];
    SI.Power dnFace[nSegments];
    SI.Power dtFace[nSegments];
    Boolean faceActive[nSegments];
    Boolean faceSelectionImproves[nSegments];
    Integer contactFace[nSegments];

    SI.Distance distanceVertex[nPoints];
    Real vertexNx[nPoints];
    Real vertexNz[nPoints];
    SI.Length deltaVertex[nPoints];
    SI.Velocity vnVertex[nPoints];
    SI.Velocity vtVertex[nPoints];
    SI.Force feVertex[nPoints];
    SI.Force fdVertex[nPoints];
    SI.Force fnVertex[nPoints];
    SI.Force ftVertex[nPoints];
    SI.Power dnVertex[nPoints];
    SI.Power dtVertex[nPoints];
    Boolean vertexActive[nPoints];
    Boolean vertexSelectionImproves[nPoints];
    Integer contactVertex[nPoints];
    Boolean terrainSegmentDefined[nSegments];
    Boolean terrainDefinedPrefix[nSegments + 1];
    Boolean terrainDefined;
    Boolean insideTerrain;
    SI.Position terrainCandidate[nSegments];
    SI.Position terrainTopPrefix[nSegments + 1];
    SI.Position terrainTop;
    SI.Length selectedPenetrationFace[nSegments + 1];
    SI.Velocity selectedSlipFace[nSegments + 1];
    Integer selectedFeatureFace[nSegments + 1];
    SI.Length selectedPenetrationVertex[nPoints + 1];
    SI.Velocity selectedSlipVertex[nPoints + 1];
    Integer selectedFeatureVertex[nPoints + 1];
    SI.Torque contactTorqueY;
    MB.Forces.WorldForceAndTorque load(
      resolveInFrame=MB.Types.ResolveInFrameB.world,
      animation=false);

  equation
    terrainDefinedPrefix[1] = false;
    terrainTopPrefix[1] = 0;
    selectedPenetrationFace[1] = 0;
    selectedSlipFace[1] = 0;
    selectedFeatureFace[1] = 0;

    for i in 1:nSegments loop
      terrainSegmentDefined[i] =
        xPoints[i + 1] - xPoints[i] > Modelica.Constants.small and
        xCenter >= xPoints[i] and xCenter <= xPoints[i + 1];
      terrainCandidate[i] = if terrainSegmentDefined[i] then
        zPoints[i] + (zPoints[i + 1] - zPoints[i])*
        (xCenter - xPoints[i])/
        max(xPoints[i + 1] - xPoints[i], Modelica.Constants.small) else 0;
      terrainDefinedPrefix[i + 1] =
        terrainDefinedPrefix[i] or terrainSegmentDefined[i];
      terrainTopPrefix[i + 1] = if terrainSegmentDefined[i] then
        (if terrainDefinedPrefix[i] then
          max(terrainTopPrefix[i], terrainCandidate[i])
         else terrainCandidate[i])
        else terrainTopPrefix[i];

      projection[i] =
        (xCenter - xPoints[i])*tx[i] + (zCenter - zPoints[i])*tz[i];
      distanceFace[i] =
        (xCenter - xPoints[i])*nx[i] + (zCenter - zPoints[i])*nz[i];
      faceActive[i] = projection[i] >= 0 and
                      projection[i] < segmentLength[i] and
                      not (vertexKind[i] == 1 and projection[i] <= 0) and
                      distanceFace[i] >= 0 and distanceFace[i] < R;
      deltaFace[i] = if faceActive[i] then
        max(0, R - distanceFace[i]) else 0;
      vnFace[i] = if faceActive[i] then
        vxCenter*nx[i] + vzCenter*nz[i] else 0;
      vtFace[i] = if faceActive[i] then
        vxCenter*nz[i] - vzCenter*nx[i] - R*omegaAbsolute else 0;
      feFace[i] = if faceActive[i] then k*deltaFace[i] else 0;
      fdFace[i] = if faceActive[i] then
        max(-feFace[i], min(feFace[i], -c*vnFace[i])) else 0;
      fnFace[i] = if faceActive[i] then feFace[i] + fdFace[i] else 0;
      ftFace[i] = if faceActive[i] then
        -mu*fnFace[i]*tanh(vtFace[i]/vreg) else 0;
      dnFace[i] = if faceActive[i] then fdFace[i]*(-vnFace[i]) else 0;
      dtFace[i] = if faceActive[i] then -ftFace[i]*vtFace[i] else 0;
      contactFace[i] = if fnFace[i] > 0 then 1 else 0;

      faceSelectionImproves[i] =
        deltaFace[i] > selectedPenetrationFace[i];
      selectedPenetrationFace[i + 1] = if faceSelectionImproves[i]
        then deltaFace[i] else selectedPenetrationFace[i];
      selectedSlipFace[i + 1] = if faceSelectionImproves[i]
        then vtFace[i] else selectedSlipFace[i];
      selectedFeatureFace[i + 1] = if faceSelectionImproves[i]
        then 100 + i else selectedFeatureFace[i];
    end for;

    terrainDefined = terrainDefinedPrefix[nSegments + 1];
    terrainTop = terrainTopPrefix[nSegments + 1];
    insideTerrain = terrainDefined and zCenter <= terrainTop;

    for i in 1:nPoints loop
      distanceVertex[i] =
        sqrt((xCenter - xPoints[i])^2 + (zCenter - zPoints[i])^2);
      vertexNx[i] = if distanceVertex[i] > Modelica.Constants.small then
        (xCenter - xPoints[i])/distanceVertex[i] else 0;
      vertexNz[i] = if distanceVertex[i] > Modelica.Constants.small then
        (zCenter - zPoints[i])/distanceVertex[i] else 0;

      deltaVertex[i] = if vertexActive[i] then
        max(0, R - distanceVertex[i]) else 0;
      vnVertex[i] = if vertexActive[i] then
        vxCenter*vertexNx[i] + vzCenter*vertexNz[i] else 0;
      vtVertex[i] = if vertexActive[i] then
        vxCenter*vertexNz[i] - vzCenter*vertexNx[i] - R*omegaAbsolute else 0;
      feVertex[i] = if vertexActive[i] then k*deltaVertex[i] else 0;
      fdVertex[i] = if vertexActive[i] then
        max(-feVertex[i], min(feVertex[i], -c*vnVertex[i])) else 0;
      fnVertex[i] = if vertexActive[i] then
        feVertex[i] + fdVertex[i] else 0;
      ftVertex[i] = if vertexActive[i] then
        -mu*fnVertex[i]*tanh(vtVertex[i]/vreg) else 0;
      dnVertex[i] = if vertexActive[i] then
        fdVertex[i]*(-vnVertex[i]) else 0;
      dtVertex[i] = if vertexActive[i] then
        -ftVertex[i]*vtVertex[i] else 0;
      contactVertex[i] = if fnVertex[i] > 0 then 1 else 0;
    end for;

    vertexActive[1] = false;
    vertexActive[nPoints] = false;
    for i in 2:nPoints - 1 loop
      vertexActive[i] = vertexKind[i] == 1 and
        distanceVertex[i] > Modelica.Constants.small and
        distanceVertex[i] < R and
        vertexNx[i]*nx[i - 1] + vertexNz[i]*nz[i - 1] >= 0 and
        vertexNx[i]*nx[i] + vertexNz[i]*nz[i] >= 0;
    end for;

    selectedPenetrationVertex[1] = selectedPenetrationFace[nSegments + 1];
    selectedSlipVertex[1] = selectedSlipFace[nSegments + 1];
    selectedFeatureVertex[1] = selectedFeatureFace[nSegments + 1];
    for i in 1:nPoints loop
      vertexSelectionImproves[i] =
        deltaVertex[i] > selectedPenetrationVertex[i];
      selectedPenetrationVertex[i + 1] = if vertexSelectionImproves[i]
        then deltaVertex[i] else selectedPenetrationVertex[i];
      selectedSlipVertex[i + 1] = if vertexSelectionImproves[i]
        then vtVertex[i] else selectedSlipVertex[i];
      selectedFeatureVertex[i + 1] = if vertexSelectionImproves[i]
        then 200 + i else selectedFeatureVertex[i];
    end for;

    Fx = sum({fnFace[i]*nx[i] + ftFace[i]*nz[i]
      for i in 1:nSegments}) +
      sum({fnVertex[i]*vertexNx[i] + ftVertex[i]*vertexNz[i]
      for i in 1:nPoints});
    Fz = sum({fnFace[i]*nz[i] - ftFace[i]*nx[i]
      for i in 1:nSegments}) +
      sum({fnVertex[i]*vertexNz[i] - ftVertex[i]*vertexNx[i]
      for i in 1:nPoints});
    normal = sum(fnFace) + sum(fnVertex);
    contactCount = sum(contactFace) + sum(contactVertex);
    elasticEnergy = 0.5*k*(sum({deltaFace[i]^2
      for i in 1:nSegments}) + sum({deltaVertex[i]^2
      for i in 1:nPoints}));
    normalLossPower = sum(dnFace) + sum(dnVertex);
    frictionLossPower = sum(dtFace) + sum(dtVertex);
    contactTorqueY = -R*(sum(ftFace) + sum(ftVertex));
    penetration = selectedPenetrationVertex[nPoints + 1];
    slip = selectedSlipVertex[nPoints + 1];
    featureId = selectedFeatureVertex[nPoints + 1];

    assert(min(segmentLength) > Modelica.Constants.small,
      "PolylineWheelContact requires nonzero directed segments");
    assert(min({xPoints[i + 1] - xPoints[i] for i in 1:nSegments}) >= 0,
      "PolylineWheelContact requires nondecreasing x coordinates");
    assert(not insideTerrain,
      "Wheel centre entered the terrain solid; fixture is physically invalid");
    assert(normalLossPower >= -1e-10 and frictionLossPower >= -1e-10,
      "Contact dissipation must be nonnegative");
    xCenter = frame.r_0[1];
    zCenter = frame.r_0[3];
    vxCenter = der(xCenter);
    vzCenter = der(zCenter);
    normalLoss = normalLossPower;
    frictionLoss = frictionLossPower;
    der(normalDissipatedEnergy) = normalLossPower;
    der(frictionDissipatedEnergy) = frictionLossPower;
    load.force = {Fx,0,Fz};
    load.torque = {0,contactTorqueY,0};
    connect(load.frame_b, frame);
  end PolylineWheelContact;

  model WheelPair
    "Equivalent left/right synchronous wheel pair with a real MSL revolute bearing"
    parameter SI.Mass m = 2;
    parameter SI.Inertia J = 0.01;
    parameter SI.Radius R = 0.1;
    parameter SI.TranslationalSpringConstant k = 100000;
    parameter SI.TranslationalDampingConstant c = 200;
    parameter Real mu = 0;
    parameter SI.Velocity vreg = 0.001;
    parameter Integer nPoints(min=2) = 2;
    parameter SI.Position xPoints[nPoints] = {-100,100};
    parameter SI.Position zPoints[nPoints] = {0,0};
    parameter Integer vertexKind[nPoints] = fill(0, nPoints);
    parameter SI.Angle phi0 = 0;
    parameter SI.AngularVelocity omega0 = 0;

    MB.Interfaces.Frame_a carrier;
    Rot.Interfaces.Flange_a axis "Bearing axis; positive MSL angle is about -y";
    Rot.Interfaces.Flange_b support;

    MB.Joints.Revolute bearing(
      n={0,-1,0},
      useAxisFlange=true,
      animation=false,
      phi(start=-phi0, fixed=true),
      w(start=-omega0, fixed=true),
      stateSelect=StateSelect.always);
    RigidBody2D wheelBody(m=m, J=J);
    PolylineWheelContact contact(
      nPoints=nPoints,
      xPoints=xPoints,
      zPoints=zPoints,
      vertexKind=vertexKind,
      R=R,
      k=k,
      c=c,
      mu=mu,
      vreg=vreg);

    output SI.AngularVelocity omega = -wheelBody.pitchRate
      "Absolute roll speed about world +y";
    output SI.AngularVelocity omegaRelative = -bearing.w;
  equation
    connect(carrier, bearing.frame_a);
    connect(bearing.frame_b, wheelBody.frame);
    connect(wheelBody.frame, contact.frame);
    connect(bearing.axis, axis);
    connect(bearing.support, support);
    contact.omegaAbsolute = omega;
  end WheelPair;

  model IdealDriveSupply
    "Supported ideal paired torque source and non-regenerative DC energy account"
    parameter SI.Torque ratedTorque = 0.2;
    parameter SI.Power ratedMechanicalPower = 4;
    parameter SI.Voltage voltage = 24;
    parameter Real efficiency(min=Modelica.Constants.small, max=1) = 0.8;
    parameter SI.Power supplyPower = 10;
    parameter SI.Energy initialEnergy = 100;

    Rot.Interfaces.Flange_b shaft;
    Rot.Interfaces.Support support;
    Modelica.Blocks.Interfaces.RealInput torqueCommand(unit="N.m")
      "Paired roll torque about +y";
    Rot.Sources.Torque source(useSupport=true);

    output SI.AngularVelocity omegaRelative =
      -(der(shaft.phi) - der(support.phi));
    output SI.Power shaftPower = torqueCommand*omegaRelative;
    output SI.Power dcPower = max(shaftPower, 0)/efficiency;
    output SI.Power driveLoss = dcPower - shaftPower;
    output SI.Power driveLossPower = driveLoss;
    output SI.Energy driveDissipatedEnergy(start=0, fixed=true);
    output SI.Energy energyAvailable(start=initialEnergy, fixed=true);
    output SI.Current busCurrent = dcPower/voltage;
  equation
    source.tau = -torqueCommand;
    connect(source.flange, shaft);
    connect(source.support, support);
    der(driveDissipatedEnergy) = driveLoss;
    der(energyAvailable) = -dcPower;
    assert(abs(torqueCommand) <= ratedTorque + 1e-10,
      "Drive torque exceeds paired continuous rating");
    assert(abs(shaftPower) <= ratedMechanicalPower + 1e-8,
      "Drive shaft power exceeds paired continuous rating");
    assert(dcPower <= supplyPower + 1e-8,
      "DC bus power exceeds supply rating");
    assert(energyAvailable >= -1e-8,
      "DC usable energy exhausted");
    assert(driveLossPower >= -1e-10,
      "Non-regenerative drive loss must be nonnegative");
  end IdealDriveSupply;

  model PlanarWithPitchAxis
    "MSL Planar thin wrapper exposing its revolute axis at this component boundary"
    extends MB.Joints.Planar(revolute(useAxisFlange=true));
    Rot.Interfaces.Flange_a pitchAxis;
    output SI.Torque pitchConstraintTorque = revolute.tau;
  equation
    connect(revolute.axis, pitchAxis);
  end PlanarWithPitchAxis;

  model PlanarWheelFixture
    "Massless nonrotating carrier, real MSL Planar joint, and one equivalent wheel pair"
    parameter SI.Position x0 = 0;
    parameter SI.Position z0 = 0.12;
    parameter SI.Velocity vx0 = 0;
    parameter SI.Velocity vz0 = 0;
    parameter SI.AngularVelocity omega0 = 0;
    parameter SI.Acceleration g = 9.81;
    parameter Real mu = 0;
    parameter Integer nPoints(min=2) = 2;
    parameter SI.Position xPoints[nPoints] = {-100,100};
    parameter SI.Position zPoints[nPoints] = {0,0};
    parameter Integer vertexKind[nPoints] = fill(0, nPoints);

    MB.Interfaces.Frame_a frameWorld;
    Rot.Interfaces.Flange_a axis "Public drive axis for the fixture wheel bearing";
    Rot.Interfaces.Flange_b support "Public relative shaft support";
    PlanarWithPitchAxis planar(
      n={0,-1,0},
      n_x={1,0,0},
      animation=false,
      s_x(start=x0, fixed=true),
      s_y(start=z0, fixed=true),
      v_x(start=vx0, fixed=true),
      v_y(start=vz0, fixed=true),
      phi(start=0, fixed=false),
      stateSelect=StateSelect.prefer);
    Rot.Components.Fixed carrierPitchLock(phi0=0);
    WheelPair wheel(
      m=2,
      J=0.01,
      R=0.1,
      k=100000,
      c=200,
      mu=mu,
      vreg=0.001,
      nPoints=nPoints,
      xPoints=xPoints,
      zPoints=zPoints,
      vertexKind=vertexKind,
      omega0=omega0);
    output SI.Energy kineticEnergy = wheel.wheelBody.kineticEnergy;
    output SI.Energy potentialEnergy = 2*g*wheel.wheelBody.z;
    output SI.Torque carrierConstraintTorque = planar.pitchConstraintTorque;
  equation
    connect(frameWorld, planar.frame_a);
    connect(planar.frame_b, wheel.carrier);
    connect(planar.pitchAxis, carrierPitchLock.flange);
    connect(wheel.axis, axis);
    connect(wheel.support, support);
  end PlanarWheelFixture;

  model B01PassiveJoint
    "B01: true MSL passive revolute, rigid rod and fixed end payload"
    extends ScenarioOutputs;
    parameter SI.Mass rodMass = 2;
    parameter SI.Length rodLength = 0.5;
    parameter SI.Inertia rodJ = rodMass*rodLength^2/12;
    parameter SI.Mass payloadMass = 1;
    parameter SI.Inertia payloadJ = 0.0025;
    parameter SI.Acceleration g = 9.81;

    inner MB.World world(
      gravityType=MB.Types.GravityTypes.UniformGravity,
      g=g,
      n={0,0,-1},
      enableAnimation=false);
    MB.Joints.Revolute hinge(
      n={0,-1,0},
      useAxisFlange=false,
      animation=false,
      phi(start=0.02, fixed=true),
      w(start=0, fixed=true),
      stateSelect=StateSelect.always);
    MB.Parts.FixedTranslation rodCentre(
      r={0,0,-rodLength/2}, animation=false);
    MB.Parts.FixedTranslation rodEnd(
      r={0,0,-rodLength}, animation=false);
    RigidBody2D rod(m=rodMass, J=rodJ);
    RigidBody2D payload(m=payloadMass, J=payloadJ);

    output SI.Angle theta = hinge.phi;
    output SI.AngularVelocity jointOmega = hinge.w;
    output SI.AngularAcceleration alpha = hinge.a;
    SI.Force pivotForceWorld[3] =
      -Frames.resolve1(hinge.frame_b.R, hinge.frame_b.f);
    Real payloadAxisWorld[3] = Frames.resolve1(payload.frame.R, {1,0,0});
    Real rodEndAxisWorld[3] = Frames.resolve1(rodEnd.frame_b.R, {1,0,0});
    output SI.Force pivotFx = pivotForceWorld[1];
    output SI.Force pivotFz = pivotForceWorld[3];
    output SI.Torque jointTorque = hinge.tau;
    output SI.Length fixedPositionResidual = sqrt(
      (payload.frame.r_0[1] - rodEnd.frame_b.r_0[1])^2 +
      (payload.frame.r_0[2] - rodEnd.frame_b.r_0[2])^2 +
      (payload.frame.r_0[3] - rodEnd.frame_b.r_0[3])^2);
    output SI.Angle fixedAngleResidual = sqrt(
      (payloadAxisWorld[1] - rodEndAxisWorld[1])^2 +
      (payloadAxisWorld[2] - rodEndAxisWorld[2])^2 +
      (payloadAxisWorld[3] - rodEndAxisWorld[3])^2)
      "Chord-angle residual; equals angle to first order near zero";
  equation
    connect(world.frame_b, hinge.frame_a);
    connect(hinge.frame_b, rodCentre.frame_a);
    connect(rodCentre.frame_b, rod.frame);
    connect(hinge.frame_b, rodEnd.frame_a);
    connect(rodEnd.frame_b, payload.frame);

    x = payload.x;
    z = payload.z;
    vx = payload.vx;
    vz = payload.vz;
    omega = jointOmega;
    Fx = 0;
    Fz = 0;
    normal = 0;
    penetration = 0;
    slip = 0;
    normalLoss = 0;
    frictionLoss = 0;
    elasticEnergy = 0;
    kineticEnergy = rod.kineticEnergy + payload.kineticEnergy;
    potentialEnergy = rodMass*g*rod.z + payloadMass*g*payload.z;
    shaftPower = 0;
    dcPower = 0;
    driveLoss = 0;
    energyAvailable = 0;
    driveTorque = 0;
    supportTorque = 0;
    contactCount = 0;
    featureId = 0;
    normalLossPower = 0;
    frictionLossPower = 0;
    driveLossPower = 0;
    normalDissipatedEnergy = 0;
    frictionDissipatedEnergy = 0;
    driveDissipatedEnergy = 0;
  end B01PassiveJoint;

  model B02DropStatic
    "B02: wheel-pair drop onto a finite flat face and static settling"
    extends ScenarioOutputs;
    parameter SI.Acceleration g = 9.81;
    inner MB.World world(
      gravityType=MB.Types.GravityTypes.UniformGravity,
      g=g,
      n={0,0,-1},
      enableAnimation=false);
    PlanarWheelFixture rig(
      x0=0, z0=0.12, vx0=0, vz0=0, omega0=0,
      g=g, mu=0,
      nPoints=2,
      xPoints={-2,2},
      zPoints={0,0},
      vertexKind={0,0});
  equation
    connect(world.frame_b, rig.frameWorld);
    x = rig.wheel.wheelBody.x;
    z = rig.wheel.wheelBody.z;
    vx = rig.wheel.wheelBody.vx;
    vz = rig.wheel.wheelBody.vz;
    omega = rig.wheel.omega;
    Fx = rig.wheel.contact.Fx;
    Fz = rig.wheel.contact.Fz;
    normal = rig.wheel.contact.normal;
    penetration = rig.wheel.contact.penetration;
    slip = rig.wheel.contact.slip;
    normalLoss = rig.wheel.contact.normalLoss;
    frictionLoss = rig.wheel.contact.frictionLoss;
    elasticEnergy = rig.wheel.contact.elasticEnergy;
    kineticEnergy = rig.kineticEnergy;
    potentialEnergy = rig.potentialEnergy;
    shaftPower = 0;
    dcPower = 0;
    driveLoss = 0;
    energyAvailable = 0;
    driveTorque = 0;
    supportTorque = rig.carrierConstraintTorque;
    contactCount = rig.wheel.contact.contactCount;
    featureId = rig.wheel.contact.featureId;
    normalLossPower = rig.wheel.contact.normalLossPower;
    frictionLossPower = rig.wheel.contact.frictionLossPower;
    driveLossPower = 0;
    normalDissipatedEnergy = rig.wheel.contact.normalDissipatedEnergy;
    frictionDissipatedEnergy = rig.wheel.contact.frictionDissipatedEnergy;
    driveDissipatedEnergy = 0;
  end B02DropStatic;

  model B03DriveBrake
    "B03: paired drive, braking and coast on a flat face"
    extends ScenarioOutputs;
    parameter SI.Acceleration g = 9.81;
    parameter SI.Position zStatic = 0.1 - 2*g/100000;
    inner MB.World world(
      gravityType=MB.Types.GravityTypes.UniformGravity,
      g=g,
      n={0,0,-1},
      enableAnimation=false);
    PlanarWheelFixture rig(
      x0=0, z0=zStatic, vx0=0, vz0=0, omega0=0,
      g=g, mu=0.6,
      nPoints=2,
      xPoints={-2,4},
      zPoints={0,0},
      vertexKind={0,0});
    IdealDriveSupply drive(
      ratedTorque=0.2,
      ratedMechanicalPower=4,
      voltage=24,
      efficiency=0.8,
      supplyPower=10,
      initialEnergy=100);
    SI.Torque tauSchedule;
  equation
    connect(world.frame_b, rig.frameWorld);
    connect(drive.shaft, rig.axis);
    connect(drive.support, rig.support);
    tauSchedule = if time < 0.1 then 2*time else
                  if time < 1.4 then 0.2 else
                  if time < 1.5 then 0.2 - 4*(time - 1.4) else
                  if time < 2.4 then -0.2 else
                  if time < 2.5 then -0.2 + 2*(time - 2.4) else 0;
    drive.torqueCommand = tauSchedule;

    x = rig.wheel.wheelBody.x;
    z = rig.wheel.wheelBody.z;
    vx = rig.wheel.wheelBody.vx;
    vz = rig.wheel.wheelBody.vz;
    omega = rig.wheel.omega;
    Fx = rig.wheel.contact.Fx;
    Fz = rig.wheel.contact.Fz;
    normal = rig.wheel.contact.normal;
    penetration = rig.wheel.contact.penetration;
    slip = rig.wheel.contact.slip;
    normalLoss = rig.wheel.contact.normalLoss;
    frictionLoss = rig.wheel.contact.frictionLoss;
    elasticEnergy = rig.wheel.contact.elasticEnergy;
    kineticEnergy = rig.kineticEnergy;
    potentialEnergy = rig.potentialEnergy;
    shaftPower = drive.shaftPower;
    dcPower = drive.dcPower;
    driveLoss = drive.driveLoss;
    energyAvailable = drive.energyAvailable;
    driveTorque = tauSchedule;
    supportTorque = rig.carrierConstraintTorque;
    contactCount = rig.wheel.contact.contactCount;
    featureId = rig.wheel.contact.featureId;
    normalLossPower = rig.wheel.contact.normalLossPower;
    frictionLossPower = rig.wheel.contact.frictionLossPower;
    driveLossPower = drive.driveLossPower;
    normalDissipatedEnergy = rig.wheel.contact.normalDissipatedEnergy;
    frictionDissipatedEnergy = rig.wheel.contact.frictionDissipatedEnergy;
    driveDissipatedEnergy = drive.driveDissipatedEnergy;
  end B03DriveBrake;

  model B04RiserImpact
    "B04: zero-gravity impact on the rising vertical face"
    extends ScenarioOutputs;
    inner MB.World world(
      gravityType=MB.Types.GravityTypes.NoGravity,
      enableAnimation=false);
    PlanarWheelFixture rig(
      x0=-0.15, z0=0.15, vx0=0.2, vz0=0, omega0=0,
      g=0, mu=0,
      nPoints=4,
      xPoints={-1,0,0,1},
      zPoints={0,0,0.3,0.3},
      vertexKind={0,-1,1,0});
  equation
    connect(world.frame_b, rig.frameWorld);
    x = rig.wheel.wheelBody.x;
    z = rig.wheel.wheelBody.z;
    vx = rig.wheel.wheelBody.vx;
    vz = rig.wheel.wheelBody.vz;
    omega = rig.wheel.omega;
    Fx = rig.wheel.contact.Fx;
    Fz = rig.wheel.contact.Fz;
    normal = rig.wheel.contact.normal;
    penetration = rig.wheel.contact.penetration;
    slip = rig.wheel.contact.slip;
    normalLoss = rig.wheel.contact.normalLoss;
    frictionLoss = rig.wheel.contact.frictionLoss;
    elasticEnergy = rig.wheel.contact.elasticEnergy;
    kineticEnergy = rig.kineticEnergy;
    potentialEnergy = 0;
    shaftPower = 0;
    dcPower = 0;
    driveLoss = 0;
    energyAvailable = 0;
    driveTorque = 0;
    supportTorque = rig.carrierConstraintTorque;
    contactCount = rig.wheel.contact.contactCount;
    featureId = rig.wheel.contact.featureId;
    normalLossPower = rig.wheel.contact.normalLossPower;
    frictionLossPower = rig.wheel.contact.frictionLossPower;
    driveLossPower = 0;
    normalDissipatedEnergy = rig.wheel.contact.normalDissipatedEnergy;
    frictionDissipatedEnergy = rig.wheel.contact.frictionDissipatedEnergy;
    driveDissipatedEnergy = 0;
  end B04RiserImpact;

  model B05CornerImpact
    "B05: zero-gravity radial impact on the unique convex upper corner"
    extends ScenarioOutputs;
    parameter Real invSqrt2 = 1/sqrt(2);
    inner MB.World world(
      gravityType=MB.Types.GravityTypes.NoGravity,
      enableAnimation=false);
    PlanarWheelFixture rig(
      x0=-0.15*invSqrt2,
      z0=0.3 + 0.15*invSqrt2,
      vx0=0.2*invSqrt2,
      vz0=-0.2*invSqrt2,
      omega0=0,
      g=0, mu=0,
      nPoints=4,
      xPoints={-1,0,0,1},
      zPoints={0,0,0.3,0.3},
      vertexKind={0,-1,1,0});
  equation
    connect(world.frame_b, rig.frameWorld);
    x = rig.wheel.wheelBody.x;
    z = rig.wheel.wheelBody.z;
    vx = rig.wheel.wheelBody.vx;
    vz = rig.wheel.wheelBody.vz;
    omega = rig.wheel.omega;
    Fx = rig.wheel.contact.Fx;
    Fz = rig.wheel.contact.Fz;
    normal = rig.wheel.contact.normal;
    penetration = rig.wheel.contact.penetration;
    slip = rig.wheel.contact.slip;
    normalLoss = rig.wheel.contact.normalLoss;
    frictionLoss = rig.wheel.contact.frictionLoss;
    elasticEnergy = rig.wheel.contact.elasticEnergy;
    kineticEnergy = rig.kineticEnergy;
    potentialEnergy = 0;
    shaftPower = 0;
    dcPower = 0;
    driveLoss = 0;
    energyAvailable = 0;
    driveTorque = 0;
    supportTorque = rig.carrierConstraintTorque;
    contactCount = rig.wheel.contact.contactCount;
    featureId = rig.wheel.contact.featureId;
    normalLossPower = rig.wheel.contact.normalLossPower;
    frictionLossPower = rig.wheel.contact.frictionLossPower;
    driveLossPower = 0;
    normalDissipatedEnergy = rig.wheel.contact.normalDissipatedEnergy;
    frictionDissipatedEnergy = rig.wheel.contact.frictionDissipatedEnergy;
    driveDissipatedEnergy = 0;
  end B05CornerImpact;

  model B06RollOff
    "B06: statically indented high platform, flight, and low-platform landing"
    extends ScenarioOutputs;
    parameter SI.Acceleration g = 9.81;
    parameter SI.Position z0 = 0.08 + 0.1 - 2*g/100000;
    inner MB.World world(
      gravityType=MB.Types.GravityTypes.UniformGravity,
      g=g,
      n={0,0,-1},
      enableAnimation=false);
    PlanarWheelFixture rig(
      x0=-0.2, z0=z0, vx0=2, vz0=0, omega0=20,
      g=g, mu=0.6,
      nPoints=4,
      xPoints={-1,0,0,2},
      zPoints={0.08,0.08,0,0},
      vertexKind={0,1,-1,0});
  equation
    connect(world.frame_b, rig.frameWorld);
    x = rig.wheel.wheelBody.x;
    z = rig.wheel.wheelBody.z;
    vx = rig.wheel.wheelBody.vx;
    vz = rig.wheel.wheelBody.vz;
    omega = rig.wheel.omega;
    Fx = rig.wheel.contact.Fx;
    Fz = rig.wheel.contact.Fz;
    normal = rig.wheel.contact.normal;
    penetration = rig.wheel.contact.penetration;
    slip = rig.wheel.contact.slip;
    normalLoss = rig.wheel.contact.normalLoss;
    frictionLoss = rig.wheel.contact.frictionLoss;
    elasticEnergy = rig.wheel.contact.elasticEnergy;
    kineticEnergy = rig.kineticEnergy;
    potentialEnergy = rig.potentialEnergy;
    shaftPower = 0;
    dcPower = 0;
    driveLoss = 0;
    energyAvailable = 0;
    driveTorque = 0;
    supportTorque = rig.carrierConstraintTorque;
    contactCount = rig.wheel.contact.contactCount;
    featureId = rig.wheel.contact.featureId;
    normalLossPower = rig.wheel.contact.normalLossPower;
    frictionLossPower = rig.wheel.contact.frictionLossPower;
    driveLossPower = 0;
    normalDissipatedEnergy = rig.wheel.contact.normalDissipatedEnergy;
    frictionDissipatedEnergy = rig.wheel.contact.frictionDissipatedEnergy;
    driveDissipatedEnergy = 0;
  end B06RollOff;

  annotation(
    uses(Modelica(version="4.0.0")),
    Documentation(info="<html><p>Implements protocol v11-s3-b-physics-protocol-1. This source has not by itself been compiled or physically accepted.</p></html>"));
end V11PhysicalB1;
