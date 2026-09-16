"""Read-only consistency audit of a saved V10 version, without rerunning physics."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES
from week9.src.schemas import CheckStatus, RunResult
from week10.src.trace_graph import TraceGraph
from week11.src.spec import spec_digest, validate_spec
from week11.src.store import WEEK11_ROOT, digest_json, safe_id

GENERATION_FILES = {'spec.json', 'requirement.txt', 'model.sysml', 'model.mo', 'run-result.json', 'generation-binding.json', 'trace-evidence.json'}
VERSION_FILES = GENERATION_FILES | {'confirmation.json', 'manifest.json'}


def audit(path: Path, expected_manifest_sha256: str | None = None) -> dict:
    result = {'technical': False, 'evidence': False, 'binding': False, 'issues': [],
              'limitations': ['Saved-record consistency only; no physics rerun or proof of human keystrokes.',
                              'Without an expected database manifest hash, coordinated replacement of all files is not independently detectable.']}
    try:
        directory = path.resolve()
        parts = directory.relative_to((WEEK11_ROOT / 'projects').resolve()).parts
        if len(parts) != 3 or parts[1] != 'versions':
            raise ValueError('not a project version directory')
        project_id, _, version_id = parts
        safe_id(project_id)
        safe_id(version_id)
        def read(name: str) -> bytes:
            candidate = (directory / name).resolve()
            candidate.relative_to(directory)
            return candidate.read_bytes()
        def load(name: str):
            return json.loads(read(name).decode('utf-8'))
        def check_files(manifest: dict, required: set[str]) -> None:
            files = manifest.get('files')
            if not isinstance(files, dict) or set(files) != required:
                raise ValueError('manifest required file set differs')
            for name, metadata in files.items():
                raw = read(name)
                if metadata.get('bytes') != len(raw) or metadata.get('sha256') != hashlib.sha256(raw).hexdigest():
                    raise ValueError('manifest file mismatch: ' + name)
        manifest_raw = read('version-manifest.json')
        if expected_manifest_sha256 and hashlib.sha256(manifest_raw).hexdigest() != expected_manifest_sha256:
            raise ValueError('database manifest hash mismatch')
        manifest = load('version-manifest.json')
        if manifest.get('schema_version') != 'v10-version-manifest-1':
            raise ValueError('version manifest schema mismatch')
        check_files(manifest, VERSION_FILES)
        generation = load('manifest.json')
        if generation.get('schema_version') != 'v10-generation-manifest-1':
            raise ValueError('generation manifest schema mismatch')
        check_files(generation, GENERATION_FILES)
        checked = validate_spec(load('spec.json'))
        if not checked['ready']:
            raise ValueError('specification invalid')
        spec = checked['spec']
        digest = spec_digest(spec)
        run = RunResult.from_dict(load('run-result.json'))
        checks = {check.check_type: check for check in run.checks}
        if run.version != 'v8' or run.case_id != project_id or not run.overall_success:
            raise ValueError('RunResult identity/status mismatch')
        for name in (*REQUIRED_HARD_CHECK_TYPES, 'traceability_integrity'):
            if name not in checks or not checks[name].required or checks[name].status is not CheckStatus.PASS:
                raise ValueError('missing/failed required check: ' + name)
        if any(check.required and check.status is not CheckStatus.PASS for check in run.checks):
            raise ValueError('a required check failed')
        result['technical'] = True
        binding = load('generation-binding.json')
        if binding.get('schema_version') != 'v10-generation-binding-1' or binding.get('spec_digest') != digest or binding.get('project_id') != project_id or binding.get('run_id') != run.run_id:
            raise ValueError('generation identity binding mismatch')
        if binding.get('run_result_payload_sha256') != digest_json(run.to_dict()) or binding.get('normalized_parameters') != spec['normalized_parameters']:
            raise ValueError('generation parameter or RunResult digest mismatch')
        if binding.get('derivations') != spec['derivations'] or binding.get('stimulus') != spec['stimulus']:
            raise ValueError('generation formula/stimulus mismatch')
        for kind, filename in {'sysml': 'model.sysml', 'modelica': 'model.mo', 'requirement': 'requirement.txt'}.items():
            raw = read(filename)
            artifact = run.artifacts[kind]
            if artifact.content.encode('utf-8') != raw or artifact.sha256 != hashlib.sha256(raw).hexdigest() or binding['artifact_sha256'].get(kind) != artifact.sha256:
                raise ValueError('model artifact binding mismatch: ' + kind)
        confirmation = load('confirmation.json')
        if confirmation.get('schema_version') != 'v10-confirmation-1' or confirmation.get('project_id') != project_id or confirmation.get('spec_digest') != digest:
            raise ValueError('confirmation scope mismatch')
        safe_id(confirmation.get('proposal_id'))
        if confirmation.get('operation') not in {'create', 'modify'} or not confirmation.get('confirmed_at') or len(confirmation.get('displayed_digest', '')) != 64:
            raise ValueError('confirmation record incomplete')
        if (confirmation['operation'] == 'create') != (confirmation.get('baseline_version_id') is None):
            raise ValueError('confirmation baseline mismatch')
        result['binding'] = True
        if run.traceability is None or not run.traceability.integrity_passed:
            raise ValueError('traceability incomplete')
        evidence = load('trace-evidence.json')
        graph = TraceGraph()
        graph.add_run(run)
        if evidence.get('schema_version') != 'v10-trace-evidence-1' or evidence.get('spec_digest') != digest or evidence.get('run_id') != run.run_id or evidence.get('trace_graph') != graph.to_dict():
            raise ValueError('saved trace evidence differs from rebuilt graph')
        result['evidence'] = True
        result.update(project_id=project_id, version_id=version_id, run_id=run.run_id, spec_digest=digest, manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(), required_checks=len(REQUIRED_HARD_CHECK_TYPES)+1)
        result['limitations'].append('The confirmation display payload is stored in SQLite; this directory alone cannot recompute its display digest.')
    except Exception as exc:
        result['issues'].append(f'{type(exc).__name__}: {exc}')
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version_directory', type=Path)
    parser.add_argument('--expected-manifest-sha256')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(args.version_directory, args.expected_manifest_sha256)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = args.output.resolve()
        output.relative_to(WEEK11_ROOT.resolve())
        output.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0 if all(result[key] for key in ('technical', 'evidence', 'binding')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
