"""Seal reviewed public-port repair; preserve accepted B01 dependency closure."""
import ast
import difflib
import hashlib
import json
from pathlib import Path
import re

from week12.src.component_physics.contract import ROOT, canonical, digest, require_dependency_hashes

DIRECTORY = ROOT/'results/s3-b-connector-repair-20260923-01'
PREVIOUS = ROOT/'results/s3-b-postprocess-repair-20260923-01'
RUNS = ROOT/'results/s3-b-postprocess-validation-20260923-01'
PACKAGE = ROOT/'models/components/V11PhysicalB1/package.mo'
RUNNER = ROOT/'src/component_physics/runner.py'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def row(path):
    sha = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024*1024), b''):
            sha.update(block)
    return {'path': str(path.resolve()), 'sha256': sha.hexdigest(), 'bytes': path.stat().st_size}


def write_new(name, value):
    with (DIRECTORY/name).open('xb') as handle:
        handle.write(canonical(value))


def model(source, name):
    match = re.search(r'  (?:partial )?model '+re.escape(name)+r'\b.*?  end '+re.escape(name)+r';', source, re.S)
    if not match:
        raise ValueError('Missing class '+name)
    return match.group(0)


def main():
    review_path = DIRECTORY/'independent-review.json'
    review = read(review_path)
    if review['unresolved_blockers']:
        raise ValueError('Independent source review has unresolved blockers')
    previous = read(PREVIOUS/'implementation-manifest.json')
    changed = {PACKAGE.resolve(), RUNNER.resolve()}
    unchanged = [item for item in previous if Path(item['path']).resolve() not in changed]
    require_dependency_hashes(unchanged)
    for path in (PACKAGE, RUNNER):
        expected = next(item for item in previous if Path(item['path']).resolve() == path.resolve())
        backup = DIRECTORY/'before'/path.name
        if row(backup)['sha256'] != expected['sha256']:
            raise ValueError('Backup identity differs from executed source')
        old, new = backup.read_text(encoding='utf-8'), path.read_text(encoding='utf-8')
        with (DIRECTORY/(path.name+'.patch')).open('x', encoding='utf-8') as handle:
            handle.write(''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                    fromfile='executed/'+path.name, tofile='repaired/'+path.name)))
    old = (DIRECTORY/'before/package.mo').read_text(encoding='utf-8')
    new = PACKAGE.read_text(encoding='utf-8')
    preserved = ['ScenarioOutputs', 'RigidBody2D', 'PolylineWheelContact', 'WheelPair', 'IdealDriveSupply',
                 'B01PassiveJoint', 'B02DropStatic', 'B04RiserImpact', 'B05CornerImpact', 'B06RollOff']
    for name in preserved:
        if model(old, name) != model(new, name):
            raise ValueError('Unexpected physical class change: '+name)
    b03 = model(old, 'B03DriveBrake').replace('rig.wheel.axis', 'rig.axis').replace('rig.wheel.support', 'rig.support')
    if b03 != model(new, 'B03DriveBrake'):
        raise ValueError('B03 changed beyond the two public port references')
    if old.split('  partial model ScenarioOutputs')[0] != new.split('  partial model ScenarioOutputs')[0]:
        raise ValueError('Shared imports changed')
    # This package contains simple scalar connectors, not nested connector records.
    # This structural audit only rejects the known illegal cross-model shape;
    # it is not an independent Modelica parser or proof of equation balance.
    connections = re.findall(r'connect\(([^;]+)\);', new)
    for statement in connections:
        for target in statement.split(','):
            if target.strip().count('.') > 1:
                raise ValueError('Cross-model connector reference remains: '+target)
    histories = read(PREVIOUS/'historical-manifest.json')
    require_dependency_hashes(histories)
    current_evidence = [row(path) for path in RUNS.glob('*.json')]
    current_evidence += [row(path) for path in (RUNS/'B01-reprocessed').glob('*.json')]
    current_evidence += [row(path) for path in (RUNS/'B02-base').iterdir() if path.is_file()]
    histories += current_evidence
    reuse = read(PREVIOUS/'reuse-input-manifest.json')
    require_dependency_hashes(reuse)
    reuse += [row(path) for path in (RUNS/'B01-reprocessed').glob('*.json')]
    reuse += [row(RUNS/'B01-report-equivalence.json')]
    extras = [PACKAGE, RUNNER, ROOT/'tests/test_component_physics_connector_report.py',
              ROOT/'scripts/s3_b_connector_execute.py', Path(__file__), review_path]
    for path in extras:
        if path.suffix == '.py':
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    sources = unchanged+[row(path) for path in extras]
    proposal = read(PREVIOUS/'execution-proposal.json')
    proposal.update(status='PENDING_USER_APPROVAL_NOT_EXECUTABLE', receipt_kind=None, user_message=None,
                    implementation_manifest_digest=digest(sources), reuse_input_digest=digest(reuse),
                    historical_attempts=5, attempt_limit=10, proposed_cumulative_attempt_ceiling=15,
                    local_consumed=15, local_additional_limit=1, local_proposed_cumulative_ceiling=16,
                    local_regression='R04_empty_check_result_with_error_is_failure',
                    reprocessing_limit=0,
                    first_attempt_role='B02 base validates the public connector repair, then its refined run and B03-B06 in order.',
                    execution_directory='results/s3-b-connector-validation-20260923-01',
                    note='B01 is accepted from existing simulation and numerical evidence for its unchanged class/dependency closure. New batch after five retained attempts; no hidden retries.')
    write_new('implementation-manifest.json', sources)
    write_new('historical-manifest.json', histories)
    write_new('reuse-input-manifest.json', reuse)
    write_new('execution-proposal.json', proposal)
    write_new('preparation-status.json', {
        'status': 'CONNECTOR_REPAIR_IMPLEMENTED_RUNTIME_UNVERIFIED',
        'preserved_Modelica_classes': preserved, 'B01_transitive_local_classes': ['ScenarioOutputs', 'RigidBody2D', 'B01PassiveJoint'],
        'B01_shared_imports_and_MSL_snapshot_unchanged': True,
        'B03_only_two_public_port_references_changed': True,
        'checked_simple_connect_statements': len(connections),
        'independent_review': str(review_path), 'unchanged_previously_frozen_files': len(unchanged),
        'new_local_executions': 0, 'new_Modelica_attempts': 0, 'online_product_calls': 0,
        'physical_equations_and_thresholds_unchanged': True,
    })
    write_new('diagnosis.json', {
        'observed_failure': 'B02 checkModel rejects rig.planar.revolute.axis crossing two non-connector model levels',
        'observed_error_log': str(RUNS/'B02-base/build.stdout.log'),
        'same_pattern_identified_before_execution': ['B03 rig.wheel.axis', 'B03 rig.wheel.support'],
        'repair': 'Expose revolute axis via PlanarWithPitchAxis and wheel axis/support via PlanarWheelFixture; connect only at immediate component boundaries.',
        'prior_B01_reprocessing_seconds': read(RUNS/'B01-reprocessing-process.json')['duration_s'],
        'prior_B01_numeric': read(RUNS/'B01-reprocessed/numerical.json')['status'],
        'prior_B01_physical_reports_equal': read(RUNS/'B01-report-equivalence.json')['exact_physical_report_equality'],
        'current_counts': {'local': 15, 'scene_attempts': 5, 'simulations': 2, 'reprocessings': 1, 'online': 0},
        'new_Modelica_execution': 0,
    })
    print('Connector repair sealed; B01 local dependency closure unchanged; no tests, reprocessing or Modelica executed; proposal PENDING.')


if __name__ == '__main__':
    main()
