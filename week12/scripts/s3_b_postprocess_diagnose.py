"""Record the stopped batch from existing evidence only; never re-run a checker."""
import hashlib
import json
from pathlib import Path

from week12.src.component_physics.contract import ROOT, canonical, require_dependency_hashes


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def hashed(path):
    sha = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            sha.update(block)
    return {'path': str(path.resolve()), 'sha256': sha.hexdigest(), 'bytes': path.stat().st_size}


def main():
    runs = ROOT/'results/s3-b-script-repair-validation-20260923-01'
    repair = ROOT/'results/s3-b-script-repair-20260923-01'
    output = ROOT/'results/s3-b-postprocess-diagnosis-20260923-01'
    require_dependency_hashes(read(repair/'implementation-manifest.json'))
    require_dependency_hashes(read(repair/'historical-manifest.json'))
    reports = {level: read(runs/('B01-'+level)/'result.json') for level in ('base', 'refined')}
    physical = {level: read(runs/('B01-'+level)/'physical.json') for level in reports}
    batch = read(runs/'batch-result.json')
    output.mkdir(exist_ok=False)
    evidence = []
    for level in reports:
        directory = runs/('B01-'+level)
        for name in ('result.json', 'physical.json', 'result.csv', 'build.mos',
                     'build.stdout.log', 'build.stderr.log', 'simulation.stdout.log',
                     'simulation.stderr.log', 'verification.stdout.log', 'verification.stderr.log'):
            evidence.append(hashed(directory/name))
    for path in sorted(runs.glob('*.json')):
        evidence.append(hashed(path))
    diagnosis = {
        'status': 'STOPPED_POSTPROCESS_TIMEOUT_NO_RERUN',
        'build_script_repair': 'B01 base and refined both built and simulated successfully',
        'local_regression': read(runs/'local-result.json')['status'],
        'individual_physical_status': {key: value['status'] for key, value in physical.items()},
        'numerical_status': 'UNVERIFIED_NO_NUMERICAL_JSON',
        'numeric_output_exists': (runs/'B01-refined/numerical.json').exists(),
        'csv_files': [item for item in evidence if item['path'].endswith('result.csv')],
        'base_verification_seconds': reports['base']['verification_process']['duration_s'],
        'refined_verification_process': reports['refined']['verification_process'],
        'failure_boundary': '30-second postprocessor wall-clock limit after refined physical.json was written',
        'source_observation': 's3_b_verify.main verifies refined once; compare_cases calls _verify on base and refined again. Each _read_csv materializes all rows, floats and columns.',
        'inference': 'Repeated parsing of approximately 145 MB CSVs is a concrete performance defect consistent with the timeout. No profiler or diagnostic rerun was executed.',
        'proposed_next_change': 'Stream all raw rows, retain needed columns, and compute each physical report once before comparison; preserve duplicate event rows, finite-value checks, numerical thresholds and the 30-second limit.',
        'reuse_boundary': 'Preserve both successful simulation CSVs. Any approved reprocessing must write new evidence and bind original hashes; do not overwrite the failed result or re-simulate B01 merely for this checker defect.',
        'budget': {'local_consumed': 13, 'local_limit': 13, 'historical_attempts_before_batch': 2,
                   'new_attempts': 2, 'cumulative_attempts': 4, 'simulations_started': 2,
                   'remaining_scenes_not_run': batch['not_run'], 'online_product_calls': 0,
                   'automatic_retry_authorized': False},
        'new_execution_during_diagnosis': 0,
        'input_evidence': evidence,
    }
    (output/'diagnosis.json').write_bytes(canonical(diagnosis))
    print('Timeout diagnosis saved; frozen sources/history unchanged; no checker or Modelica rerun.')


if __name__ == '__main__':
    main()
