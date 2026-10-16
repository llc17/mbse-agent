"""Independent analytic witnesses; no simulated vehicle success is fabricated."""
import csv
import io
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from week12.src.integration import vehicle_verify as v
from week12.src.integration.timing_amendment import inputs as approved_inputs


class CVehicleVerifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _,cls.bundle,cls.contract=approved_inputs()

    def test_probe_analytic_power_rotor_energy_and_negative_witness(self):
        times=tuple(i/1000 for i in range(2001))
        cols={'time':times,'probeShaftPower':(30.,)*len(times),'probeDcPower':(37.5,)*len(times),
              'probeDriveLoss':(7.5,)*len(times),'probeBusPower':(42.5,)*len(times),
              'probeBusCurrent':(42.5/48,)*len(times),'probeBusEnergy':tuple(100-42.5*t for t in times)}
        checks=[]; metrics={}; v._verify_shared_probe(v._Data(cols,len(times)),checks,metrics)
        self.assertEqual(v._finish(checks,metrics,[])['status'],'PASS')
        cols['probeBusEnergy']=tuple(100-40*t for t in times)
        checks=[]; v._verify_shared_probe(v._Data(cols,len(times)),checks,{})
        self.assertEqual(v._finish(checks,{},[])['status'],'FAIL')
        # Independent closed solution: sinh(w/.1)=sinh(100)*exp(-50t).
        omega=tuple(.1*math.asinh(math.sinh(100)*math.exp(-50*t)) for t in times)
        torque=tuple(-.05*math.tanh(w/.1) for w in omega)
        loss=tuple(-t*w for t,w in zip(torque,omega))
        data=v._Data({'time':times,'probeOmega':omega,'probeLoss':loss,'probeKinetic':tuple(.005*w*w for w in omega),
                      'probeRollingTorque':torque,'probeRollingLoss':loss},len(times))
        checks=[]; v._verify_rolling_probe(data,checks,{})
        self.assertEqual(v._finish(checks,{},[])['status'],'PASS')
        self.assertAlmostEqual(omega[-1],.1*math.asinh(.5),places=10)

    def test_independent_mechanical_energy_detects_unaccounted_work(self):
        c={'id':'body','count':1,'mass':{'value':2},'inertia':{'value':.5}}
        w={'id':'wheel','count':2,'mass':{'value':1},'inertia':{'value':.01}}
        bundle={'design':{'bodies':[c],'wheels':[w],'drives':[]},'physical':{}}
        cols={'time':(0.,1.)}
        for item in (c,w):
            for key in ('x','z','vx','vz','pitch','pitchRate'): cols['body_'+item['id']+'_'+key]=(0.,0.)
        cols['body_body_vx']=(0.,1.)
        cols['body_wheel_vx']=(0.,1.)
        cols['wheel_wheel_omega']=(0.,0.)
        for key in ('elasticEnergy','shaftPower','normalLoss','frictionLoss','rollingLoss'): cols['wheel_wheel_'+key]=(0.,0.)
        metrics,residual=v._vehicle_energy(bundle,v._Data(cols,2))
        self.assertAlmostEqual(residual[-1],2.)  # .5*(2+2)*1^2
        cols['wheel_wheel_shaftPower']=(2.,2.)
        metrics,residual=v._vehicle_energy(bundle,v._Data(cols,2))
        self.assertAlmostEqual(residual[-1],0.)

    def test_envelope_crossing_corner_feature_and_event_boundary(self):
        terrain=v._terrain([[-1,0],[0,0],[0,.3],[1,.3]])
        shape={'type':'polygon','vertices':[[-.1,-.01],[.1,-.01],[.1,.01],[-.1,.01]]}
        self.assertLess(v._shape_clearance(shape,(0,.15,0),terrain),0)
        self.assertGreater(v._shape_clearance(shape,(0,.5,0),terrain),0)
        pen,features=v._wheel_geometric_penetration(.5,.39,.1,terrain)
        self.assertAlmostEqual(pen,.01)
        self.assertIn(103,features)
        self.assertEqual(v._crossings((0.,1.,1.,2.),(-1.,0.,0.,1.),0,1),[1.])
        self.assertAlmostEqual(v._exit_threshold({'exit_plane_x_m':15.2,'exit_margin_m':.5},terrain),15.7)
        capsule={'type':'capsule','start':[0,-.04],'end':[0,.04],'radius':.03}
        self.assertLess(v._shape_clearance(capsule,(-.02,.15,0),terrain),0)
        ts=tuple(i/1000 for i in range(301))
        dat=v._Data({'time':ts,'body_front_z':(3.127,)*len(ts),'body_front_vz':(0.,)*len(ts)},len(ts))
        window=v._last_support_window(dat,'front',[t<.25 for t in ts],.3)
        self.assertAlmostEqual(window[0],.249)
        self.assertIsNone(v._last_support_window(dat,'front',[t<.05 for t in ts],.3))

    def test_numerical_gate_and_missing_or_incomplete_csv_rejected(self):
        report={'status':'PASS','metrics':{'signals':{'omega':{'peak_abs':1.,'terminal':1.}},'event_times':{'drop':1.},'cumulative':{}}}
        other={'status':'PASS','metrics':{'signals':{'omega':{'peak_abs':1.,'terminal':1.}},'event_times':{'drop':1.003},'cumulative':{}}}
        self.assertEqual(v._compare_reports(self.contract,report,other)['status'],'FAIL')
        for content in ('time,x\n0,0\n1,0\n','time\n0\n.5\n'):
            with patch.object(Path,'open',return_value=io.StringIO(content)):
                with self.assertRaises(v._EvidenceProblem): v._read_csv(Path('unused'),{'time','probeOmega'},2,.001)
        with patch.object(Path,'open',return_value=io.StringIO('time\n0\n.001\n')):
            with self.assertRaises(v._EvidenceProblem): v._read_csv(Path('unused'),{'time'},2,.001)


if __name__=='__main__': unittest.main()
