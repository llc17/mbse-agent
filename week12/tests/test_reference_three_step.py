"""Only the new input, its binding and refusal boundaries; no dynamics here."""
from copy import deepcopy
from pathlib import Path
import tempfile, unittest
from week12.src.car_domain import reference_three_profile as p
from week12.src.car_domain.reference_generator import render_model
from week12.src.car_domain.reference_three_generator import generate_reference
from week12.src.car_domain.reference_three_verify import _source_parameter_readback

def make_spec(source_id='src_current_chat'):
    # Normalized known template requirements are explicitly labelled interpretation,
    # separate from the retained current-chat excerpts; no old20 quote is rewritten.
    text=('Current-chat approved template reuse; structured interpretation of retained reference: '
          'total_mass 85 kg; payload_mass 20 kg; step_height 0.15 m; step_depth 0.28 m. '
          '本轮用户：把楼梯改为3级；确认继续。continuous_steps 3 count. '
          'reuse reference template; '+p.TASK_NAME)
    rows=[]
    for name,(value,unit) in p.CORE.items():
        snippet=p.TASK_NAME if unit=='task' else f'{name} {value} {unit}'
        rows.append(dict(name=name,value=value,unit=unit,source_id=source_id,source_text=snippet,origin='user',classification='hard'))
    return dict(schema_version=p.SPEC_SCHEMA,profile=p.PROFILE,
        reference_ref=dict(path=p.SOURCE_PATH,sha256=p.SOURCE_SHA),parameters=p.parameter_values(),
        sources=[dict(id=source_id,text=text)],requirements=rows,
        design_permission=dict(mode='REUSE_USER_REFERENCE_TEMPLATE',source_id=source_id,source_text='reuse reference template'),
        constraints=[],preferences=[],ideas=[],assumptions=[])

class ThreeStepTests(unittest.TestCase):
    def test_actual_slot_and_derived_values(self):
        spec=make_spec();self.assertTrue(p.validate_reference_spec(spec)['ready'])
        raw=render_model(spec['parameters']);self.assertNotEqual(raw,p.source_bytes())
        values=_source_parameter_readback(raw)
        self.assertEqual(values['n_steps'],3)
        self.assertAlmostEqual(values['H_peak'],.45)
        self.assertAlmostEqual(values['x_end_down'],5.68)
        self.assertEqual(raw.replace(b'n_steps = 3;',b'n_steps = 20;'),p.source_bytes())
    def test_sysml_and_source_map(self):
        import json
        parent=p.REPO_ROOT/'week12/results/s3-de-reference-three-step-20261011-01'
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            result=generate_reference(make_spec(),Path(folder)/'generated')
            self.assertIn('n_steps : ScalarValues::Integer = 3;',Path(result['sysml_path']).read_text(encoding='utf8'))
            mapped=json.loads(Path(result['parameter_map_path']).read_bytes())
            self.assertEqual(mapped['literal_values']['n_steps'],3)
            self.assertEqual(next(x for x in mapped['parameters'] if x['name']=='n_steps')['expression'],'3')
    def test_invalid_inputs_rejected(self):
        for value in (0,3.5,True,20):
            spec=make_spec();spec['parameters']['n_steps']=value
            self.assertFalse(p.validate_reference_spec(spec)['ready'])
    def test_other_physics_and_source_locked(self):
        spec=make_spec();spec['parameters']['m']=86
        self.assertFalse(p.validate_reference_spec(spec)['ready'])
        spec=make_spec();spec['reference_ref']['sha256']='0'*64
        self.assertFalse(p.validate_reference_spec(spec)['ready'])
    def test_closed_scope_refused(self):
        from week12.src.car_domain import reference_three_route as route
        with self.assertRaisesRegex(route.Stop,'NO_REOPEN'):
            route.assert_scope(dict(status='CLOSED'))
    def test_original_render_is_byte_identical(self):
        from week12.src.car_domain.reference_profile import parameter_values
        self.assertEqual(render_model(parameter_values()),p.source_bytes())

if __name__=='__main__':unittest.main()
