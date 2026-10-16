"""Read-only dependency snapshot and the remaining one bounded OMC help probe."""
import hashlib
import json
from pathlib import Path
import time

from week12.src.component_physics.contract import ROOT, canonical, require_dependency_hashes, PROTOCOL, PROTOCOL_SHA
from week12.src.component_physics.runner import MSL, COMPLEX, SERVICES, OMC
from week12.src.toolchain import _run

DIRECTORY = ROOT/'results/s3-b-preflight-20260923-01'


def hashed(path):
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes': path.stat().st_size}


def main():
    local = json.loads((ROOT/'results/s3-b-local-remaining-20260923-01/result.json').read_text(encoding='utf-8'))
    if local['status'] != 'LOCAL_PASS' or local['consumed'] != 12:
        raise ValueError('Local gate not satisfied')
    selected = json.loads((ROOT/'results/s3-b-development-20260922-01/implementation-manifest.json').read_text(encoding='utf-8'))
    require_dependency_hashes(selected)
    if hashlib.sha256(PROTOCOL.read_bytes()).hexdigest() != PROTOCOL_SHA:
        raise ValueError('Protocol drift')
    DIRECTORY.mkdir(exist_ok=False)
    # Preparation already observed omc --version once. This reserves the second
    # and last permitted read-only version/flag probe before process creation.
    command = [str(OMC), '--help=simulation']
    (DIRECTORY/'probe-reservation.json').write_bytes(canonical({
        'prior_version_probes': 1, 'probe_limit': 2, 'reserved_probe': 2,
        'command': command, 'timeout_seconds': 20, 'status': 'RESERVED',
        'scene_attempts': 0, 'purpose': 'read_only_compiler_runtime_flag_help'}))
    probe = _run(command, DIRECTORY, 20)
    for stream in ('stdout', 'stderr'):
        (DIRECTORY/('help.'+stream+'.log')).write_text(probe[stream], encoding='utf-8')
    (DIRECTORY/'probe-result.json').write_bytes(canonical({k: v for k, v in probe.items() if k not in ('stdout', 'stderr')}))
    text = probe['stdout']+'\n'+probe['stderr']
    help_ok = probe['returncode'] == 0 and not probe['timed_out'] and not probe['uncertain'] and 'maxStepSize' in text and 'dassl' in text.lower()
    if not help_ok:
        (DIRECTORY/'status.json').write_bytes(canonical({'status': 'BLOCKED', 'reason': 'runtime_help_probe_failed', 'retry_allowed': False}))
        print('BLOCKED: help evidence unavailable; no retry.')
        return 1
    started = time.monotonic()
    dependencies = []
    # Complete installed contents of exactly these three dependency packages.
    # This is not a recursive scan of project history or private candidate data.
    for package in (MSL, COMPLEX, SERVICES):
        for path in sorted(package.parent.rglob('*')):
            if path.is_file():
                dependencies.append(hashed(path))
    (DIRECTORY/'installed-library-manifest.json').write_bytes(canonical(dependencies))
    omc = hashed(OMC)
    (DIRECTORY/'tool-identity.json').write_bytes(canonical({
        'omc': omc, 'prior_observed_version': 'OpenModelica v1.25.7 (64-bit)',
        'actual_runtime_flag_probe': command,
        'boundary': 'Installed library contents frozen; compiler installation/native toolchain not fully snapshotted.'}))
    report = {'status': 'PREFLIGHT_PASS_PENDING_EXECUTION_APPROVAL', 'local_cases_passed': 12,
              'local_case_limit_consumed': 12, 'read_only_probe_total': 2,
              'new_modelica_scene_attempts': 0, 'new_simulations': 0, 'online_product_calls': 0,
              'installed_library_file_count': len(dependencies),
              'installed_library_bytes': sum(row['bytes'] for row in dependencies),
              'library_snapshot_seconds': time.monotonic()-started,
              'flag_evidence': {'maxStepSize': True, 'dassl': True},
              'numerical_status': 'UNVERIFIED_UNTIL_ACTUAL_RUN',
              'physical_status': 'UNVERIFIED', 'full_toolchain_snapshot': False}
    (DIRECTORY/'status.json').write_bytes(canonical(report))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
