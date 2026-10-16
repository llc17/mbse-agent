"""Parse new Python and recheck protected hashes; never run a test or Modelica."""
import ast
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    repo = root.parent
    sources = list((root/'src/component_physics').glob('*.py'))
    sources += list((root/'scripts').glob('s3_b_*.py'))
    sources += [root/'tests/test_component_physics_contract.py']
    parsed = []
    for path in sources:
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        parsed.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    preparation = json.loads((root/'results/s3-b-preparation-20260922-01/source-manifest.json').read_text(encoding='utf-8'))
    recovery = root/'results/s3-b-recovery-20260922-01'
    history = json.loads((recovery/'before-restoration.json').read_text(encoding='utf-8'))['historical_execution_files']
    for row in preparation['files'] + history:
        path = Path(row['path'])
        if not path.is_absolute():
            path = repo/path
        if hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('Protected source/history changed: '+str(path))
    protocol = root/'任务规划/B-基础物理组件与验证协议.md'
    assert hashlib.sha256(protocol.read_bytes()).hexdigest() == 'cc9875ad68b44aa1d614e54fa16226165d527b63f8b052de9ef2b0efb8f13de8'
    report = {'kind': 'source_integrity_and_python_syntax_only', 'status': 'PASS',
              'protected_sources_matched': len(preparation['files']),
              'historical_files_matched': len(history), 'protocol_unchanged': True,
              'parsed_python': parsed, 'local_cases_executed': 0,
              'new_modelica_attempts': 0, 'physical_status': 'UNVERIFIED',
              'review_notes': ['Wheel +y angular-speed sign corrected',
                               'Carrier pitch locked through actual MSL rotational flange',
                               'Pivot force is support force on pendulum',
                               'Ground support torque is opposite carrier bearing reaction',
                               'Independent energy reconstructed from observed states',
                               'Zero-gravity impact energy scale excludes gravity',
                               'B06 permits corner support after platform edge before flight'],
              'pending': ['L05/L06/L07/L08/L10', 'Modelica translation and physical validation',
                          'Runtime flag and complete dependency checks before execution']}
    with (recovery/'static-review-20260923.json').open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(f'Python parsed: {len(parsed)}; protected sources: {len(preparation["files"])}; historical files: {len(history)}; no tests or Modelica launched.')


if __name__ == '__main__':
    main()
