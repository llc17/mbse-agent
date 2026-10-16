"""Read-only diagnosis of the stopped connector batch; never launches tools."""
import hashlib
import json

from week12.src.component_physics.contract import ROOT, canonical, require_dependency_hashes


def main():
    repair = ROOT/'results/s3-b-connector-repair-20260923-01'
    batch = ROOT/'results/s3-b-connector-validation-20260923-01'
    case = batch/'B02-base'
    destination = ROOT/'results/s3-b-event-diagnosis-20260923-01'
    for name in ('implementation-manifest.json', 'historical-manifest.json', 'reuse-input-manifest.json'):
        require_dependency_hashes(json.loads((repair/name).read_text(encoding='utf-8')))
    result = json.loads((case/'result.json').read_text(encoding='utf-8'))
    stdout = (case/'build.stdout.log').read_text(encoding='utf-8')
    generated = (case/'model_05evt.c').read_text(encoding='utf-8')
    source = (case/'V11PhysicalB1.mo').read_text(encoding='utf-8')
    if result['model_check'] != 'PASS' or "error: use of undeclared identifier 'rig'" not in stdout:
        raise ValueError('Diagnosis does not match observed failure')
    paths = [repair/'authorization-record.json', batch/'batch-result.json', batch/'local-result.json',
             batch/'attempt-01.json', case/'result.json', case/'build.stdout.log', case/'build.stderr.log',
             case/'V11PhysicalB1.mo', case/'model.c', case/'model_05evt.c', case/'model_06inz.c']
    evidence = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                 'bytes': path.stat().st_size} for path in paths]
    record = {
        'status': 'STOPPED_FIRST_FAILURE_READ_ONLY_DIAGNOSIS',
        'observed': {
            'local_R04': 'PASS', 'load_file': 'PASS', 'checkModel': 'PASS',
            'equations': 767, 'variables': 767,
            'build': 'FAIL_C_COMPILER_ERROR', 'simulation_started': False,
            'compiler_error': "model_05evt.c: use of undeclared identifier 'rig'",
            'compiler_error_count': stdout.count("error: use of undeclared identifier 'rig'"),
            'generated_bad_reference_lines': [i for i, line in enumerate(generated.splitlines(), 1)
                                              if 'rig._wheel._contact._n' in line],
            'source_guard_lines': [i for i, line in enumerate(source.splitlines(), 1)
                                    if 'if i > 1 and i < nPoints then' in line],
            'flat_fixture': 'B02 nPoints=2, nSegments=1: no interior convex vertex',
            'generated_event_issue': 'Event code nevertheless contains endpoint angular-sector relations: nx[0]/nz[0] at first point and nx[2]/nz[2] at last point; latter becomes an undeclared rig record reference.',
        },
        'interpretation': 'Known failure boundary is generated C event code. Source bounds depend on an algorithm if guard; emitted zero-crossing relations include indices outside segment range. Exact compiler internal cause and workaround effectiveness are not independently verified.',
        'next_repair_candidate': 'Separate vertex geometry and force loops from an interior-only 2:nPoints-1 activation loop; initialize endpoint activation false. Preserve all interior sector tests, contact laws and event detection. Review empty-loop handling for nPoints=2 and multi-segment equivalence before proposing bounded runtime validation.',
        'not_done': ['No active source repair', 'No generated C patch', 'No noEvent suppression',
                     'No compiler/library upgrade', 'No test or Modelica retry', 'No new budget approval'],
        'preserved': 'Approved implementation, historical manifests and B01 accepted evidence hashes verified unchanged',
        'counts': {'local': 16, 'scene_attempts': 6, 'simulations': 2, 'CSV_reprocessing': 1,
                   'read_only_tool_probes': 2, 'online_product_calls': 0},
        'remaining_current_batch': 'Nine entries NOT_RUN; stopped batch cannot resume',
    }
    destination.mkdir(exist_ok=False)
    for name, value in [('diagnosis.json', record), ('evidence-manifest.json', evidence)]:
        with (destination/name).open('xb') as handle:
            handle.write(canonical(value))
    print('Existing evidence diagnosed; source/dependency/history hashes unchanged; no new execution.')


if __name__ == '__main__':
    main()
