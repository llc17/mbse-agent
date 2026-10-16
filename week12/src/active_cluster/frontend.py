"""Prepared, non-executing front-end contract for the registered C3 scenes.

This module only creates script text and checks returned evidence. It never
starts a process, grants execution authority, changes a vehicle or resumes C.
Actual OMC evidence is still required; Python checks are not Modelica PASS.
"""
from __future__ import annotations

from pathlib import Path
import re

from .execution import ORDER, Stop, V11


def registered_classes(manifest):
    classes = manifest.get('model_class_by_scene', {})
    if not isinstance(classes, dict) or set(classes) != set(ORDER):
        raise Stop('FRONTEND_REQUIRES_EXACT_REGISTERED_SCENE_SET')
    values = tuple(classes[name] for name in ORDER)
    if (any(not isinstance(value, str) or not
            re.fullmatch(r'V11GeneratedActive\.[A-Za-z_]\w*', value) for value in values)
            or len(set(values)) != 7):
        raise Stop('INVALID_OR_DUPLICATE_FRONTEND_CLASSES')
    return values


def _mos_path(path):
    value = Path(path).as_posix()
    if any(c in value for c in ('"', '\n', '\r')):
        raise Stop('UNSAFE_FRONTEND_PATH_LITERAL')
    return value


def frontend_script(manifest):
    """Load pinned libraries, then check all seven classes once, without build."""
    from week12.src.component_physics.runner import COMPLEX, SERVICES, MSL
    classes = registered_classes(manifest)
    package = Path(manifest['package_path'])
    if not (package.name == 'V11GeneratedActive.mo' or
            package.name == 'package.mo' and package.parent.name == 'V11GeneratedActive'):
        raise Stop('FRONTEND_PACKAGE_FILENAME_ROOT_MISMATCH')
    dependencies = (COMPLEX, SERVICES, MSL,
                    V11/'models/components/V11PhysicalB1/package.mo',
                    V11/'models/components/V11ContactC2/package.mo',
                    V11/'models/components/V11ActiveClusterC3/package.mo', package)
    lines = []
    for i, path in enumerate(dependencies):
        lines += [f'loaded := loadFile("{_mos_path(path)}");',
                  f'print("__GF_LOAD_{i}__="+String(loaded)+"\\n");',
                  'print(getErrorString()+"\\n");',
                  'if not loaded then exit(1); end if;']
    lines += ['print("__GF_OMC__="+getVersion()+"\\n");',
              'print("__GF_MSL__="+getVersion(Modelica)+"\\n");',
              'print("__GF_MSL_SOURCE__="+getSourceFile(Modelica)+"\\n");',
              'allChecks := true;']
    for i, cls in enumerate(classes):
        lines += [f'checked := checkModel({cls});', 'diagnostic := getErrorString();',
                  f'print("__GF_CHECK_{i}__="+checked+"\\n");',
                  f'print("__GF_ERROR_{i}_BEGIN__\\n"+diagnostic+"\\n__GF_ERROR_{i}_END__\\n");',
                  'allChecks := allChecks and regexBool(checked,"completed successfully") and not regexBool(diagnostic,"Error:");']
    lines += ['print("__GF_COMPLETE__="+String(allChecks)+"\\n");',
              'if not allChecks then exit(1); end if;']
    return '\n'.join(lines)+'\n'


def _marker(text, name):
    values = re.findall(r'^'+re.escape(name)+r'=(.*)$', text, re.M)
    return values[0].strip() if len(values) == 1 else ''


def frontend_evidence(manifest, result):
    """Check every returned marker; missing/duplicated evidence never passes."""
    from week12.src.component_physics.runner import MSL
    classes = registered_classes(manifest)
    stdout = result.get('stdout', '')
    records = []
    for i, cls in enumerate(classes):
        check = _marker(stdout, f'__GF_CHECK_{i}__')
        errors = re.findall(r'^'+re.escape(f'__GF_ERROR_{i}_BEGIN__')+
                            r'\r?\n(.*?)^'+re.escape(f'__GF_ERROR_{i}_END__')+r'\s*$',
                            stdout, re.M | re.S)
        diagnostic = errors[0] if len(errors) == 1 else ''
        passed = (len(errors) == 1 and 'completed successfully' in check.lower()
                  and not re.search(r'\bError:', diagnostic, re.I))
        records.append({'class': cls, 'status': 'PASS' if passed else 'FAIL',
                        'check_text': check, 'diagnostic': diagnostic,
                        'diagnostic_block_present_once': len(errors) == 1})
    version = _marker(stdout, '__GF_OMC__')
    msl_version = _marker(stdout, '__GF_MSL__')
    msl_source = _marker(stdout, '__GF_MSL_SOURCE__').strip('"')
    identity = (bool(re.search(r'(?<!\d)1\.25\.7(?!\d)', version)) and
                msl_version == '4.0.0' and bool(msl_source) and
                Path(msl_source).resolve() == MSL.resolve())
    loads = [_marker(stdout, f'__GF_LOAD_{i}__') == 'true' for i in range(7)]
    passed = (result.get('returncode') == 0 and not result.get('timed_out', True)
              and not result.get('uncertain', True) and identity and all(loads)
              and all(row['status'] == 'PASS' for row in records)
              and _marker(stdout, '__GF_COMPLETE__') == 'true')
    return {'status': 'PASS_FRONTEND_ONLY' if passed else 'FAIL',
            'classes': records, 'pinned_identity_verified': identity,
            'dependency_loads': loads, 'actual_OMC': version,
            'actual_MSL': msl_version, 'actual_MSL_source': msl_source,
            'compile': 'NOT_RUN', 'initialization': 'NOT_RUN',
            'simulation': 'NOT_RUN', 'vehicle_task': 'NOT_RUN',
            'execution_authority': 'No authority granted by this evidence parser.'}
