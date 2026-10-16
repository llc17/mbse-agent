"""Fixed subset of seven authorized local B checks; no Modelica and no A reruns."""
from pathlib import Path
import io
import json
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from week12.tests.test_component_physics_contract import BPreparationChecks, EVIDENCE

NAMES = ['L01_coordinate_virtual_work', 'L02_single_to_pair_and_offset',
         'L03_absolute_relative_speed_and_power', 'L04_riser_outward_normals',
         'L09_descriptor_change_and_product_rejection', 'L11_dependency_content_drift',
         'L12_reservation_conflict_and_interruption']


def main():
    EVIDENCE.mkdir(exist_ok=False)
    start = time.monotonic()
    state = {'kind': 'local_contract_checks_only', 'limit': 12, 'consumed': 0,
             'modelica_attempts': 0, 'online_calls': 0, 'cases': [],
             'not_run': ['L05', 'L06', 'L07', 'L08', 'L10'], 'physical_status': 'UNVERIFIED'}
    for name in NAMES:
        if time.monotonic()-start >= 120:
            state['stop_reason'] = 'local_time_budget'
            break
        state['consumed'] += 1
        item = {'case': name, 'status': 'RESERVED'}
        state['cases'].append(item)
        (EVIDENCE/'result.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(BPreparationChecks('test_'+name))
        (EVIDENCE/(name+'.log')).write_text(stream.getvalue(), encoding='utf-8')
        item['status'] = 'PASS' if result.wasSuccessful() else 'FAIL'
        (EVIDENCE/'result.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        if not result.wasSuccessful():
            state['stop_reason'] = 'first_failure_no_retry'
            break
    state['status'] = 'PARTIAL_LOCAL_PASS' if len(state['cases']) == 7 and all(c['status']=='PASS' for c in state['cases']) else 'STOPPED'
    state['elapsed_seconds'] = time.monotonic()-start
    (EVIDENCE/'result.json').write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(state, ensure_ascii=False))
    return 0 if state['status'] == 'PARTIAL_LOCAL_PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
