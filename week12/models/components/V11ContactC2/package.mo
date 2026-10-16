within;
package V11ContactC2
  "Unvalidated C contact ownership candidate; accepted B1 is unchanged"
  import SI = Modelica.Units.SI;
  import MB = Modelica.Mechanics.MultiBody;
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
    Boolean afterStart[nSegments];
    Boolean beforeEnd[nSegments];
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
    for j in 1:nSegments loop
      assert((xPoints[j + 1] == xPoints[j] or zPoints[j + 1] == zPoints[j]) and
             segmentLength[j] > Modelica.Constants.small,
             "C2 ownership repair supports axis-aligned terrain only");
    end for;
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
      afterStart[i] = projection[i] > 0;
      beforeEnd[i] = projection[i] < segmentLength[i];
      faceActive[i] = (if vertexKind[i] == 1 then afterStart[i] else projection[i] >= 0) and
                      beforeEnd[i] and
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
      contactFace[i] = if faceActive[i] and fnFace[i] > 0 then 1 else 0;

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
      contactVertex[i] = if vertexActive[i] and fnVertex[i] > 0 then 1 else 0;
    end for;

    vertexActive[1] = false;
    vertexActive[nPoints] = false;
    for i in 2:nPoints - 1 loop
      vertexActive[i] = vertexKind[i] == 1 and
        distanceVertex[i] > Modelica.Constants.small and
        distanceVertex[i] < R and
        not beforeEnd[i - 1] and not afterStart[i];
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
end V11ContactC2;
