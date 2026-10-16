"""B-only pinned build/execute boundary; no legacy whole-car model generation.

Importing this module does not execute tools. Callers must supply a separately
approved concrete batch; a restored source tree is never treated as authorization.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time

from .contract import ROOT, CLASS_NAMES, canonical, digest, fixture, require_frozen_fixture, require_dependency_hashes
from .ledger import Ledger

LIBRARIES = Path('C:/Users/13169/AppData/Roaming/.openmodelica/libraries')
MSL = LIBRARIES / 'Modelica 4.0.0+maint.om/package.mo'
COMPLEX = LIBRARIES / 'Complex 4.0.0+maint.om/package.mo'
SERVICES = LIBRARIES / 'ModelicaServices 4.0.0+maint.om/package.mo'
PACKAGE = ROOT / 'models/components/V11PhysicalB1/package.mo'
OMC = Path('D:/openmodelica/bin/omc.exe')


def build_script(ident, *, refined=False):
    data = require_frozen_fixture(fixture(ident))
    name, stop = data['model_class'], data['stop']
    tolerance = 1e-7 if refined else 1e-6
    # Paths are explicit, forward-slash, fixed local dependencies; no loadModel defaults.
    return f'''complexOK := loadFile("{COMPLEX.as_posix()}");
print("__B_COMPLEX__=" + String(complexOK) + "\\n");
print("__B_COMPLEX_ERROR__=" + getErrorString() + "\\n");
if not complexOK then
  exit(1);
end if;
servicesOK := loadFile("{SERVICES.as_posix()}");
print("__B_SERVICES__=" + String(servicesOK) + "\\n");
print("__B_SERVICES_ERROR__=" + getErrorString() + "\\n");
if not servicesOK then
  exit(1);
end if;
mslOK := loadFile("{MSL.as_posix()}");
print("__B_MSL__=" + String(mslOK) + "\\n");
print("__B_MSL_ERROR__=" + getErrorString() + "\\n");
if not mslOK then
  exit(1);
end if;
print("__B_MSL_VERSION__=" + getVersion(Modelica) + "\\n");
print("__B_MSL_SOURCE__=" + getSourceFile(Modelica) + "\\n");
loadedFile := loadFile("V11PhysicalB1.mo");
print("__B_LOAD_FILE__=" + String(loadedFile) + "\\n");
print("__B_LOAD_ERROR__=" + getErrorString() + "\\n");
if not loadedFile then
  exit(1);
end if;
check := checkModel({name});
print("__B_CHECK__=" + check + "\\n");
print("__B_CHECK_ERROR__=" + getErrorString() + "\\n");
checkOK := regexBool(check, "completed successfully");
if not checkOK then
  exit(1);
end if;
print("__B_BUILD_STARTED__=true\\n");
build := buildModel({name},startTime=0,stopTime={stop:.17g},numberOfIntervals={round(stop/.0001)},tolerance={tolerance:.17g},method="dassl",outputFormat="csv",fileNamePrefix="model");
print("__B_BUILD_EXE__=" + build[1] + "\\n");
print("__B_BUILD_ERROR__=" + getErrorString() + "\\n");
'''


def build_observations(stdout):
    """Read stage evidence even when the overall compiler process has failed.

    Missing markers are NOT_OBSERVED: truncated output cannot prove NOT_RUN.
    A reported executable is only a string here; process and file checks below
    must pass before compile=PASS or a simulation can start.
    """
    from week12.src.toolchain import _marker

    def boolean_stage(marker):
        value = _marker(stdout, marker).lower()
        return 'PASS' if value == 'true' else 'FAIL' if value == 'false' else 'NOT_OBSERVED'

    check = _marker(stdout, '__B_CHECK__')
    return {
        'dependency_load': {name: boolean_stage('__B_'+name+'__')
                            for name in ('COMPLEX', 'SERVICES', 'MSL')},
        'load_file': boolean_stage('__B_LOAD_FILE__'),
        'model_check': ('PASS' if 'completed successfully' in check.lower()
                        else 'FAIL' if '__B_CHECK__=' in stdout else 'NOT_OBSERVED'),
        'model_check_text': check,
        'compile': ('ATTEMPTED' if _marker(stdout, '__B_BUILD_STARTED__').lower() == 'true'
                    or '__B_BUILD_EXE__=' in stdout else 'NOT_OBSERVED'),
        'reported_executable': _marker(stdout, '__B_BUILD_EXE__'),
        'observed_msl_version': _marker(stdout, '__B_MSL_VERSION__'),
        'observed_msl_source': _marker(stdout, '__B_MSL_SOURCE__').strip('"'),
    }


def execute_attempt(ledger: Ledger, ident, *, refined, approval, frozen_sources, batch_started):
    """One attempt only; caller must stop on any non-PASS. No implicit retries."""
    data = require_frozen_fixture(fixture(ident))
    if ledger.test_only or approval.get('receipt_kind') != 'user_conversation':
        raise ValueError('PENDING: synthetic ledger cannot launch Modelica')
    if type(refined) is not bool:
        raise ValueError('INVALID: numerical setting must be explicit')
    if approval.get('implementation_manifest_digest') != digest(frozen_sources):
        raise ValueError('STALE: approval does not bind the actual implementation')
    if approval.get('descriptor_digests') != {key: fixture(key)['descriptor_digest'] for key in CLASS_NAMES}:
        raise ValueError('STALE: approval does not bind all fixture inputs')
    required = {PACKAGE.resolve(), MSL.resolve(), COMPLEX.resolve(), SERVICES.resolve(),
                Path(__file__).resolve(), (ROOT/'src/component_physics/contract.py').resolve(),
                (ROOT/'src/component_physics/ledger.py').resolve(),
                (ROOT/'src/component_physics/verify.py').resolve(),
                (ROOT/'src/component_physics/event_codegen.py').resolve(),
                (ROOT/'scripts/s3_b_verify.py').resolve()}
    observed = {(Path(row['path']) if Path(row['path']).is_absolute() else ROOT.parent/row['path']).resolve()
                for row in frozen_sources}
    if not required.issubset(observed):
        raise ValueError('INVALID: incomplete implementation/dependency snapshot')
    require_dependency_hashes(frozen_sources)
    if time.monotonic()-batch_started >= 45*60:
        raise TimeoutError('Batch wall budget exhausted before reservation')
    # These unchanged utilities only manage process execution and log parsing.
    from week12.src.toolchain import _run, _marker, _resolve_generated
    name = ident + ('-refined' if refined else '-base')
    slot = ledger.reserve(name, approval=approval)
    directory = ledger.directory / name
    report = {'scenario': name, 'status': 'FAIL', 'slot': slot,
              'load_file': 'NOT_RUN', 'model_check': 'NOT_RUN',
              'compile': 'NOT_RUN', 'simulation': 'NOT_RUN',
              'physical': 'UNVERIFIED', 'numeric': 'UNVERIFIED'}
    simulation_started = False
    owns_directory = False
    try:
        directory.mkdir(exist_ok=False)
        owns_directory = True
        (directory / 'descriptor.json').write_bytes(canonical(data))
        (directory / 'V11PhysicalB1.mo').write_bytes(PACKAGE.read_bytes())
        (directory / 'source-manifest.json').write_bytes(canonical(frozen_sources))
        (directory / 'trace-map.json').write_bytes(canonical({
            'fixture_id': ident, 'descriptor_digest': data['descriptor_digest'],
            'model_class': data['model_class'], 'physical_version': data['physical_version'],
            'mapping_version': data['mapping_version'],
            'implementation_manifest_digest': digest(frozen_sources),
            'scope': 'development_fixture_only_not_A_product_generation',
        }))
        (directory / 'build.mos').write_text(build_script(ident, refined=refined), encoding='utf-8', newline='\n')
        build_command = [str(OMC), 'build.mos']
        report['build_command'] = build_command
        report['compile'] = 'ATTEMPTED'
        build = _run(build_command, directory, min(120, 45*60-(time.monotonic()-batch_started)))
        for stream in ('stdout', 'stderr'):
            (directory / ('build.'+stream+'.log')).write_text(build[stream], encoding='utf-8')
        report['build_process'] = {k: v for k, v in build.items() if k not in ('stdout', 'stderr')}
        stdout = build['stdout']
        report.update(build_observations(stdout))
        if build['interrupted']:
            raise KeyboardInterrupt
        if build['returncode'] != 0 or build['timed_out'] or build['uncertain']:
            raise RuntimeError('BUILD_PROCESS_FAILED')
        for flag in ('COMPLEX', 'SERVICES', 'MSL'):
            if _marker(stdout, '__B_'+flag+'__').lower() != 'true':
                raise RuntimeError('DEPENDENCY_LOAD_FAILED:'+flag)
        if _marker(stdout, '__B_MSL_VERSION__') != '4.0.0':
            raise RuntimeError('DEPENDENCY_VERSION_MISMATCH')
        observed_path = _marker(stdout, '__B_MSL_SOURCE__').strip('"')
        if Path(observed_path).resolve() != MSL.resolve():
            raise RuntimeError('DEPENDENCY_SOURCE_MISMATCH')
        if report['load_file'] != 'PASS':
            raise RuntimeError('MODEL_LOAD_FAILED: see immediate __B_LOAD_ERROR__')
        if report['model_check'] != 'PASS':
            raise RuntimeError('MODEL_CHECK_FAILED')
        executable = _resolve_generated(directory, _marker(stdout, '__B_BUILD_EXE__'))
        if executable is None:
            raise RuntimeError('MODEL_BUILD_EXECUTABLE_MISSING')
        report['compile'] = 'PASS'
        from .event_codegen import audit_source
        event_report = audit_source((directory/'model_05evt.c').read_text(encoding='utf-8'))
        (directory/'event-codegen.json').write_bytes(canonical(event_report))
        report['event_codegen'] = event_report['status']
        if event_report['status'] != 'PASS':
            raise RuntimeError('EVENT_CODEGEN_RELATION_ALIAS_OR_UNVERIFIED')
        max_step = .0005 if refined else .001
        command = [str(executable), '-s=dassl', f'-maxStepSize={max_step}', '-r=result.csv']
        report['simulation_command'] = command
        # Persist the launch intention before starting the process; a crash cannot turn it into NOT_RUN.
        simulation_started = None  # unknown until _run returns, not proof of process creation
        report['simulation'] = 'LAUNCH_REQUESTED'
        (directory / 'launch.json').write_bytes(canonical({'command': command, 'simulation_launch_reserved': True}))
        simulation = _run(command, directory, min(60, 45*60-(time.monotonic()-batch_started)))
        simulation_started = (simulation['returncode'] is not None or simulation['timed_out']
                              or simulation['interrupted'])
        for stream in ('stdout', 'stderr'):
            (directory / ('simulation.'+stream+'.log')).write_text(simulation[stream], encoding='utf-8')
        report['simulation_process'] = {k: v for k, v in simulation.items() if k not in ('stdout', 'stderr')}
        if simulation['interrupted']:
            raise KeyboardInterrupt
        if simulation['returncode'] != 0 or simulation['timed_out'] or simulation['uncertain']:
            raise RuntimeError('SIMULATION_PROCESS_FAILED')
        report['simulation'] = 'COMPLETED_PENDING_CSV_CHECK'
        verify_command = [sys.executable, '-B', str(ROOT/'scripts/s3_b_verify.py'), ident, str(directory/'result.csv')]
        if refined:
            verify_command += ['--base', str(ledger.directory/(ident+'-base')/'result.csv')]
        verification = _run(verify_command, directory, min(30, 45*60-(time.monotonic()-batch_started)))
        report['verification_command'] = verify_command
        for stream in ('stdout', 'stderr'):
            (directory / ('verification.'+stream+'.log')).write_text(verification[stream], encoding='utf-8')
        report['verification_process'] = {k: v for k, v in verification.items() if k not in ('stdout', 'stderr')}
        if (directory/'physical.json').exists():
            report['physical'] = json.loads((directory/'physical.json').read_text(encoding='utf-8'))['status']
        if (directory/'numerical.json').exists():
            report['numeric'] = json.loads((directory/'numerical.json').read_text(encoding='utf-8'))['status']
        if verification['interrupted']:
            raise KeyboardInterrupt
        if verification['returncode'] != 0 or verification['timed_out'] or verification['uncertain']:
            raise RuntimeError('PHYSICAL_NUMERIC_OR_VERIFIER_FAILURE')
        physical = json.loads((directory/'physical.json').read_text(encoding='utf-8'))
        report['physical'] = physical['status']
        if physical['status'] != 'PASS':
            raise RuntimeError('PHYSICAL_OR_OUTPUT_CHECK_FAILED')
        report['simulation'] = 'PASS'
        if refined:
            comparison = json.loads((directory/'numerical.json').read_text(encoding='utf-8'))
            report['numeric'] = comparison['status']
            if comparison['status'] != 'PASS':
                raise RuntimeError('NUMERICAL_COMPARISON_FAILED')
        report['status'] = 'PASS'
    except KeyboardInterrupt:
        report.update(status='INTERRUPTED', error='USER_INTERRUPTED')
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if owns_directory:
            (directory / 'result.json').write_bytes(canonical(report))
        else:
            # Directory conflict must never overwrite a previous run's result.
            with (ledger.directory / f"attempt-{slot['batch_slot']:02d}-setup-error.json").open('xb') as handle:
                handle.write(canonical(report))
        ledger.finish(slot['batch_slot'], report['status'], simulation_started=simulation_started,
                      evidence=str(directory))
    return report
