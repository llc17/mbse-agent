"""Seal B implementation for review, without compiling, simulating, or approving it."""
from pathlib import Path
import hashlib
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from week12.src.component_physics.contract import (
    ROOT, PROTOCOL, PROTOCOL_SHA, CLASS_NAMES, canonical, digest, fixture,
    require_dependency_hashes, write_fixtures,
)
from week12.src.component_physics.runner import PACKAGE, MSL, COMPLEX, SERVICES


def main():
    directory = ROOT / 'results/s3-b-development-20260922-01'
    configs = ROOT / 'configs/component_physics/frozen-b1'
    source_manifest = json.loads((ROOT/'results/s3-b-preparation-20260922-01/source-manifest.json').read_text(encoding='utf-8'))
    local_path = ROOT/'results/s3-b-local-20260922-01/result.json'
    local = json.loads(local_path.read_text(encoding='utf-8'))
    if local.get('status') != 'PARTIAL_LOCAL_PASS' or local.get('consumed') != 7:
        raise ValueError('Local checks do not match the reviewed checkpoint')
    require_dependency_hashes(source_manifest['files'])
    if hashlib.sha256(PROTOCOL.read_bytes()).hexdigest() != PROTOCOL_SHA:
        raise ValueError('Protocol changed; no preparation output created')
    if directory.exists() or configs.exists():
        raise FileExistsError('Fixed review directory already exists; do not overwrite or change run ID')
    code = [ROOT/'src/component_physics'/name for name in ('__init__.py', 'contract.py', 'ledger.py', 'runner.py', 'verify.py')]
    code += [PACKAGE, PACKAGE.parent/'README.md',
             MSL.parent/'Mechanics/Rotational/Components/Fixed.mo',
             ROOT/'scripts/s3_b_prepare.py', ROOT/'scripts/s3_b_verify.py',
             ROOT/'scripts/s3_b_static_review.py',
             ROOT/'scripts/s3_b_local_checks.py', ROOT/'tests/test_component_physics_contract.py',
             local_path, COMPLEX, SERVICES]
    rows = list(source_manifest['files'])
    for path in code:
        data = path.read_bytes()  # Ensure complete review set exists before first write.
        rows.append({'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    directory.mkdir()
    write_fixtures(configs)
    for key in CLASS_NAMES:
        (directory/(key+'.json')).write_bytes(canonical(fixture(key)))
    (directory/'implementation-manifest.json').write_bytes(canonical(rows))
    order = [key+'-'+level for key in CLASS_NAMES for level in ('base', 'refined')]
    proposal = {'status': 'PENDING_USER_APPROVAL_NOT_EXECUTABLE',
                'purpose': 'b_component_execution', 'protocol_file_sha256': PROTOCOL_SHA,
                'receipt_kind': None,
                'implementation_manifest_digest': digest(rows),
                'descriptor_digests': {key: fixture(key)['descriptor_digest'] for key in CLASS_NAMES},
                'run_order': order, 'attempt_limit': 12, 'historical_attempts': 1,
                'proposed_cumulative_attempt_ceiling': 13, 'user_message': None,
                'online_product_calls': 0, 'retry_slots': 0,
                'local_cases_remaining': ['L05', 'L06', 'L07', 'L08', 'L10'],
                'local_limit_unchanged': 12, 'local_consumed': 7,
                'stop_on_first_failure': True, 'batch_wall_seconds': 2700,
                'per_attempt_seconds': {'build': 120, 'simulation': 60, 'postprocess': 30},
                'note': 'New finite batch proposal only. Prior failure remains counted. No tools executed.'}
    (directory/'execution-proposal.json').write_bytes(canonical(proposal))
    (directory/'status.json').write_bytes(canonical({
        'status': 'IMPLEMENTATION_PREPARED_UNVERIFIED', 'physical_status': 'UNVERIFIED',
        'tool_status': 'NOT_RUN', 'new_modelica_attempts': 0, 'new_simulations': 0,
        'local_acceptance_cases_run': 7, 'local_acceptance_cases_passed': 7, 'online_product_calls': 0,
        'dependency_closure_complete': False, 'protocol_unchanged': True,
        'no_product_vehicle_generated': True,
        'pending': ['B local L05/L06/L07/L08/L10', 'Modelica load/check/build', 'six paired physical scenes'],
    }))
    print('Prepared six frozen fixture descriptors and source manifest; no compilation or simulation.')


if __name__ == '__main__':
    main()
