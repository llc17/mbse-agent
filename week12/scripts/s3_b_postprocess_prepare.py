"""Freeze the CSV efficiency repair and its bounded, pending validation plan."""
import ast
import difflib
import hashlib
import json
from pathlib import Path

from week12.src.component_physics.contract import ROOT, canonical, digest, require_dependency_hashes

DIRECTORY = ROOT/'results/s3-b-postprocess-repair-20260923-01'
PREVIOUS = ROOT/'results/s3-b-script-repair-20260923-01'
CHANGES = [ROOT/'src/component_physics/verify.py', ROOT/'scripts/s3_b_verify.py']


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


def main():
    frozen = read(PREVIOUS/'implementation-manifest.json')
    changed_paths = {path.resolve() for path in CHANGES}
    unchanged = [item for item in frozen if Path(item['path']).resolve() not in changed_paths]
    require_dependency_hashes(unchanged)
    for path in CHANGES:
        original = next(item for item in frozen if Path(item['path']).resolve() == path.resolve())
        backup = DIRECTORY/'before'/path.name
        if row(backup)['sha256'] != original['sha256']:
            raise ValueError('Backup does not match previously executed source: '+str(path))
        before, after = backup.read_text(encoding='utf-8'), path.read_text(encoding='utf-8')
        with (DIRECTORY/(path.name+'.patch')).open('x', encoding='utf-8') as handle:
            handle.write(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                    fromfile='executed/'+path.name, tofile='repaired/'+path.name)))
    before_tree = ast.parse((DIRECTORY/'before/verify.py').read_text(encoding='utf-8'))
    after_tree = ast.parse(CHANGES[0].read_text(encoding='utf-8'))
    functions_before = {node.name: node for node in before_tree.body if isinstance(node, ast.FunctionDef)}
    functions_after = {node.name: node for node in after_tree.body if isinstance(node, ast.FunctionDef)}
    preserved = []
    for name, node in functions_before.items():
        if name not in ('_read_csv', 'compare_cases'):
            if ast.dump(node) != ast.dump(functions_after[name]):
                raise ValueError('Unapproved physical/helper algorithm change: '+name)
            preserved.append(name)
    if ([ast.dump(node) for node in functions_before['compare_cases'].body[3:]] !=
            [ast.dump(node) for node in functions_after['_compare_reports'].body[1:]]):
        raise ValueError('Numerical comparison rules changed')
    def constants(tree):
        return [ast.dump(node) for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
    if constants(before_tree) != constants(after_tree):
        raise ValueError('Verification constants changed')
    extras = CHANGES+[ROOT/'tests/test_component_physics_postprocess.py',
                      ROOT/'scripts/s3_b_postprocess_execute.py', Path(__file__)]
    for path in extras:
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    sources = unchanged+[row(path) for path in extras]
    prior_evidence = read(ROOT/'results/s3-b-postprocess-diagnosis-20260923-01/diagnosis.json')['input_evidence']
    require_dependency_hashes(prior_evidence)
    history = read(PREVIOUS/'historical-manifest.json')+prior_evidence
    require_dependency_hashes(history)
    prior_runs = ROOT/'results/s3-b-script-repair-validation-20260923-01'
    reuse = [row(prior_runs/('B01-'+level)/name) for level in ('base', 'refined')
             for name in ('result.csv', 'physical.json', 'descriptor.json', 'V11PhysicalB1.mo', 'result.json')]
    proposal = read(PREVIOUS/'execution-proposal.json')
    proposal.update(status='PENDING_USER_APPROVAL_NOT_EXECUTABLE', receipt_kind=None, user_message=None,
                    implementation_manifest_digest=digest(sources), reuse_input_digest=digest(reuse),
                    historical_attempts=4, attempt_limit=10, proposed_cumulative_attempt_ceiling=14,
                    run_order=[name for name in proposal['run_order'] if not name.startswith('B01-')],
                    local_consumed=13, local_additional_limit=2, local_proposed_cumulative_ceiling=15,
                    local_regression=['R02_stream_preserves_csv_guards_and_events', 'R03_single_read_and_fail_closed_pair'],
                    reprocessing_limit=1, reprocessing_timeout_seconds=30,
                    first_attempt_role='After local regressions and one B01 CSV-only reprocessing pass, run B02 base; no B01 recompilation or resimulation.',
                    execution_directory='results/s3-b-postprocess-validation-20260923-01',
                    physical_specification_changed=False, comparison_thresholds_changed=False,
                    note='New stopped-batch continuation proposal. Original 4 scene attempts and 2 simulations retained; CSV-only reprocessing is separately reserved and bounded, not a refunded scene attempt.')
    write_new('implementation-manifest.json', sources)
    write_new('historical-manifest.json', history)
    write_new('reuse-input-manifest.json', reuse)
    write_new('execution-proposal.json', proposal)
    write_new('preparation-status.json', {
        'status': 'EFFICIENCY_REPAIR_IMPLEMENTED_UNVERIFIED',
        'changed_existing_sources': [str(path) for path in CHANGES],
        'parsed_python_files': len(extras), 'unchanged_previously_frozen_files': len(unchanged),
        'unchanged_function_asts': preserved, 'numerical_comparison_ast_unchanged': True,
        'verification_constants_unchanged': True,
        'physical_model_unchanged': True, 'csv_originals_preserved': True,
        'new_local_executions': 0, 'new_postprocessing_executions': 0,
        'new_Modelica_attempts': 0, 'online_product_calls': 0,
    })
    print('Streaming repair sealed; physical/comparison ASTs and constants unchanged; all validation remains NOT_RUN; proposal PENDING.')


if __name__ == '__main__':
    main()
