"""Seal the control-script repair and a new finite proposal; execute no checks."""
import ast
import difflib
import hashlib
import json
from pathlib import Path

from week12.src.component_physics.contract import ROOT, canonical, digest, require_dependency_hashes
from week12.src.component_physics.runner import build_script

DIRECTORY = ROOT/'results/s3-b-script-repair-20260923-01'
RUNNER = ROOT/'src/component_physics/runner.py'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def row(path):
    data = path.read_bytes()
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def write_new(name, value):
    with (DIRECTORY/name).open('xb') as handle:
        handle.write(canonical(value))


def main():
    ready = ROOT/'results/s3-b-ready-20260923-01'
    original = read(ready/'implementation-manifest.json')
    expected_runner = next(item for item in original if Path(item['path']).resolve() == RUNNER.resolve())
    backup = DIRECTORY/'before/runner.py'
    if row(backup)['sha256'] != expected_runner['sha256']:
        raise ValueError('Repair backup does not match executed runner')
    unchanged = [item for item in original if Path(item['path']).resolve() != RUNNER.resolve()]
    require_dependency_hashes(unchanged)
    if row(RUNNER)['sha256'] == expected_runner['sha256']:
        raise ValueError('No repair to seal')
    # Only exact named historical evidence directories, no broad history scan.
    old_history = read(ROOT/'results/s3-b-recovery-20260922-01/before-restoration.json')['historical_execution_files']
    require_dependency_hashes(old_history)
    diagnosis = read(ROOT/'results/s3-b-diagnosis-20260923-01/diagnosis.json')
    require_dependency_hashes(diagnosis['input_evidence'])
    previous_root = ROOT/'results/s3-b-execution-20260923-01'
    historical = old_history+diagnosis['input_evidence']+[row(path) for path in previous_root.glob('*.json')]
    new_code = [RUNNER, ROOT/'tests/test_component_physics_script_repair.py',
                ROOT/'scripts/s3_b_script_repair_execute.py', Path(__file__)]
    for path in new_code:
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    snapshots = unchanged+[row(path) for path in new_code]
    before = backup.read_text(encoding='utf-8')
    after = RUNNER.read_text(encoding='utf-8')
    patch = ''.join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                       fromfile='executed/runner.py', tofile='repaired/runner.py'))
    with (DIRECTORY/'runner.patch').open('x', encoding='utf-8') as handle:
        handle.write(patch)
    previous_proposal = read(ROOT/'results/s3-b-development-20260922-01/execution-proposal.json')
    previews = DIRECTORY/'script-previews'
    previews.mkdir(exist_ok=False)
    for scenario in previous_proposal['run_order']:
        ident, level = scenario.split('-')
        with (previews/(scenario+'.mos')).open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(build_script(ident, refined=level=='refined'))
    write_new('implementation-manifest.json', snapshots)
    write_new('historical-manifest.json', historical)
    proposal = dict(previous_proposal)
    proposal.update(status='PENDING_USER_APPROVAL_NOT_EXECUTABLE',
                    receipt_kind=None, user_message=None,
                    implementation_manifest_digest=digest(snapshots),
                    historical_attempts=2, proposed_cumulative_attempt_ceiling=14,
                    local_consumed=12, local_cases_remaining=[],
                    local_additional_limit=1, local_proposed_cumulative_ceiling=13,
                    local_regression='R01_stage_evidence_survives_process_failure',
                    local_regression_timeout_seconds=10, additional_read_only_probes=0,
                    physical_specification_changed=False,
                    first_attempt_role='B01 base validates the repair and is the actual base run if all checks pass; no extra uncounted diagnostic.',
                    execution_directory='results/s3-b-script-repair-validation-20260923-01',
                    note='New explicitly bounded proposal after two retained historical attempts. No tests or Modelica execution during preparation.')
    # The original local 12-case budget is fully consumed; this is one new
    # regression slot, not an unrecorded extension or rerun of those cases.
    proposal.pop('local_limit_unchanged', None)
    write_new('execution-proposal.json', proposal)
    write_new('preparation-status.json', {
        'status': 'REPAIR_IMPLEMENTED_RUNTIME_UNVERIFIED',
        'changed_existing_sources': [str(RUNNER)], 'parsed_python_files': len(new_code),
        'unchanged_previously_frozen_files': len(unchanged),
        'historical_files_preserved': len(historical),
        'physical_model_and_thresholds_unchanged': True,
        'new_local_cases_run': 0, 'new_modelica_attempts': 0, 'new_simulations': 0,
        'online_product_calls': 0, 'script_previews_only': 12,
        'pending': ['New budget approval', 'R01 regression', 'B01 base then remaining ordered scenes'],
    })
    print('Repair sealed: only runner.py changed; 4 Python files parsed; regression NOT_RUN; Modelica NOT_RUN; proposal PENDING.')


if __name__ == '__main__':
    main()
