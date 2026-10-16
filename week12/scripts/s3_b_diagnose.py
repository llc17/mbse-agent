"""Read existing failed evidence and prepare an unexecuted repair candidate."""
import hashlib
from pathlib import Path

from week12.src.component_physics.contract import ROOT, canonical


def main():
    run = ROOT/'results/s3-b-execution-20260923-01/B01-base'
    directory = ROOT/'results/s3-b-diagnosis-20260923-01'
    stdout = (run/'build.stdout.log').read_text(encoding='utf-8')
    stderr = (run/'build.stderr.log').read_text(encoding='utf-8')
    original = (run/'build.mos').read_text(encoding='utf-8')
    if '__B_LOAD_FILE__=true' not in stdout or '424 equation(s) and 424 variable(s)' not in stdout:
        raise ValueError('Unexpected evidence; do not infer diagnosis')
    directory.mkdir(exist_ok=False)
    header = original.split('if complexOK and servicesOK and mslOK then')[0]
    build_statement = next(line.strip() for line in original.splitlines() if 'build := buildModel(' in line)
    candidate = header+'''if not complexOK then
  exit(1);
end if;
if not servicesOK then
  exit(1);
end if;
if not mslOK then
  exit(1);
end if;
loadedFile := loadFile("V11PhysicalB1.mo");
print("__B_LOAD_FILE__=" + String(loadedFile) + "\\n");
print("__B_LOAD_ERROR__=" + getErrorString() + "\\n");
if not loadedFile then
  exit(1);
end if;
check := checkModel(V11PhysicalB1.B01PassiveJoint);
print("__B_CHECK__=" + check + "\\n");
print("__B_CHECK_ERROR__=" + getErrorString() + "\\n");
checkOK := regexBool(check, "completed successfully");
if not checkOK then
  exit(1);
end if;
'''+build_statement+'''
print("__B_BUILD_EXE__=" + build[1] + "\\n");
print("__B_BUILD_ERROR__=" + getErrorString() + "\\n");
'''
    (directory/'build-script-candidate.mos').write_text(candidate, encoding='utf-8')
    evidence = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                for path in sorted(run.iterdir()) if path.is_file()]
    report = {
        'status': 'READ_ONLY_DIAGNOSIS_REPAIR_NOT_EXECUTED',
        'observed': {'dependency_load': 'PASS', 'Modelica_version': '4.0.0',
                     'component_load': 'PASS', 'B01_checkModel': 'PASS',
                     'equations': 424, 'variables': 424, 'process_returncode': 1,
                     'build_executable_evidence': 'ABSENT', 'simulation': 'NOT_RUN'},
        'stderr_verbatim': stderr,
        'failure_boundary': 'OMC scripting conditional evaluation after successful checkModel',
        'root_cause_status': 'NOT_FULLY_PROVEN_NO_DIAGNOSTIC_RERUN',
        'hypothesis': 'Nested scripting control and Modelica.Utilities.Strings.find call interact with the scripting evaluator.',
        'candidate_change': 'Use top-level fail-fast guards and documented scripting regexBool; preserve model, parameters, solver and thresholds.',
        'official_source': 'https://openmodelica.org/doc/OpenModelicaUsersGuide/v1.25.7/scripting_api.html#regexbool',
        'reporting_issue': 'Runner checks process return code before parsing markers; original result.json load_file=NOT_RUN understates successful load. This append-only diagnosis reconciles raw markers without rewriting the failed result.',
        'future_reporting_fix': 'Parse and persist individual stage markers before applying overall process failure.',
        'budget': {'local_consumed': 12, 'local_limit': 12, 'probe_consumed': 2, 'probe_limit': 2,
                   'historical_scene_attempts': 1, 'this_batch_attempts': 1,
                   'cumulative_scene_attempts': 2, 'simulations': 0,
                   'remaining_scenes_not_run': 11, 'automatic_retry_authorized': False},
        'candidate_applied_to_active_source': False, 'candidate_executed': False,
        'input_evidence': evidence,
    }
    (directory/'diagnosis.json').write_bytes(canonical(report))
    print('Diagnosis and unexecuted script candidate saved. No source edit, check/build, simulation or retry.')


if __name__ == '__main__':
    main()
