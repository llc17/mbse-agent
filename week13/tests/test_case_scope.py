import unittest
from week13.src.case_scope import expression_value,modelica_parameters,model_shape

class Expressions(unittest.TestCase):
    def test_parameter_dependency_preserved(self):
        values,expressions,kinds=modelica_parameters('model M parameter Real a=.22; parameter Real ext=.32; parameter Real nose=a+ext; parameter Integer count=20; end M;')
        self.assertAlmostEqual(values['nose'],.54);self.assertEqual(expressions['nose'],'a+ext');self.assertEqual(kinds['count'],'Integer')
    def test_cycle_and_unsafe_calls_rejected(self):
        for source in ['parameter Real a=b; parameter Real b=a;','parameter Real a=abs(2);','parameter Real a=1/0;','parameter Integer a=1.5;']:
            with self.subTest(source=source),self.assertRaises(ValueError):modelica_parameters(source)
    def test_external_expression_rejected(self):
        for expr in ['x.y','__import__("os")','[1][0]','2**3','float("nan")']:
            with self.subTest(expression=expr),self.assertRaises(ValueError):expression_value(expr,{})
    def test_physics_and_dependency_token_lock(self):
        original='model M parameter Real m=1; parameter Real a=.2; parameter Real b=a+.1; Real x; equation der(x)=m; annotation(experiment(StopTime=120,Tolerance=.01,Interval=.05)); end M;'
        allowed=original.replace('m=1','m=2').replace('Tolerance=.01','Tolerance=.0001')
        self.assertEqual(model_shape(original,['m']),model_shape(allowed,['m']))
        self.assertNotEqual(model_shape(original,['m']),model_shape(original.replace('der(x)=m','der(x)=m*2'),['m']))
        self.assertNotEqual(model_shape(original,['m']),model_shape(original.replace('b=a+.1','b=.3'),['m']))

if __name__=='__main__':unittest.main()
