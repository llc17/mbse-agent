"""Content-bound reuse of a worker's saved fixed-tool conclusion.

This is one result projection, not a cache. It hashes the current evidence and
checker sources, and never parses CSV, runs physics, or reads model narration.
"""
from copy import deepcopy
from pathlib import Path
import json

from week11.src.store import digest_json, safe_id
from .inherited.service_adapter import file_sha256, _verify_manifest

SCHEMA = 'v12-saved-fixed-assessment-1'
ROOT = Path(__file__).resolve().parent
TASK_KEYS = ('task_id', 'project_id', 'version_id', 'session_id', 'proposal_id', 'request_id', 'request_sha256', 'worker_owner')
RESULT_META = {'saved_assessment', 'outcome_manifest_path', 'outcome_manifest_sha256', 'worker_exit_code', 'worker_pid'}


def checker_binding(spec):
    from .case_asym2_family import is_asym2_source
    from .case_contact_review import ASYM2_CHECKER_FILES, ASYM2_RULE_VERSION
    from .case_contact_validity import RULE_VERSION
    asym2 = is_asym2_source(spec['artifact']['modelica_source'])
    files = ASYM2_CHECKER_FILES if asym2 else ('case_checks.py', 'case_contact_validity.py', 'case_contact_review.py', 'case_contact_family.py', 'inherited/model_artifact.py', 'inherited/artifact_delivery.py')
    files = tuple(files) + ('case_saved_assessment.py',)
    return dict(rule_version=ASYM2_RULE_VERSION if asym2 else RULE_VERSION,
                checker_source_sha256={name: file_sha256(ROOT / name) for name in files})


def _payload(outcome):
    return {k: v for k, v in outcome.items() if k not in RESULT_META}


def assessment_inputs(spec, base):
    """Freeze the inputs immediately before the real checker runs."""
    return dict(checker=checker_binding(spec), evidence_sha256={p.relative_to(base).as_posix(): file_sha256(p) for p in base.rglob('*') if p.is_file()})


def create_receipt(task, proposal, spec, outcome, base, ctx, *, checked_inputs):
    """Called only after the actual fixed checker, before outcome/manifest save."""
    if ctx is None:
        return None
    evidence = {p.relative_to(base).as_posix(): file_sha256(p) for p in base.rglob('*') if p.is_file()}
    current_checker = checker_binding(spec)
    if current_checker != checked_inputs['checker'] or any(evidence.get(name) != value for name, value in checked_inputs['evidence_sha256'].items()):
        raise ValueError('fixed checker sources/evidence changed during assessment')
    return dict(schema=SCHEMA, task={k: task.get(k) for k in TASK_KEYS},
                request_id=ctx['request_id'], request_digest=ctx['request_digest'],
                displayed_digest=ctx['displayed_digest'], baseline_ref=deepcopy(ctx['baseline_ref']),
                spec_digest=proposal['spec_digest'], source_sha256=proposal['display']['source_sha256'],
                assessment_sha256=digest_json(_payload(outcome)), evidence_sha256=evidence,
                checked_input_sha256=deepcopy(checked_inputs['evidence_sha256']), checker=current_checker)


def _no_links(path):
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise ValueError('saved assessment evidence link refused')


def verify_saved(service, task, ctx):
    """Verify only this current task/version; never resolve its old baselines."""
    from .inherited.contracts import inside_week12
    for key in ('task_id', 'project_id', 'proposal_id', 'version_id', 'session_id'):
        safe_id(task[key], key)
    if task['project_id'] != ctx['project_id'] or task['session_id'] != ctx['session_id']:
        raise ValueError('saved assessment request project/session differs')
    service._require_current_project(task['project_id'])
    proposal = service.store.proposal(task['proposal_id']);spec = proposal['spec'];display = proposal['display']
    source = spec['artifact']['modelica_source']
    import hashlib
    if digest_json(spec) != proposal['spec_digest'] or digest_json(display) != proposal['displayed_digest'] or hashlib.sha256(source.encode()).hexdigest() != display.get('source_sha256'):
        raise ValueError('saved assessment proposal/spec/source drift')
    if proposal.get('project_id') != task['project_id'] or proposal.get('session_id') != task['session_id']:
        raise ValueError('saved assessment proposal identity differs')
    confirmation = proposal.get('confirmation') or {}
    if confirmation.get('request_id') != ctx['request_id'] or confirmation.get('request_displayed_digest') != ctx['displayed_digest']:
        raise ValueError('saved assessment confirmation/request differs')
    declared = {s['id']: s['text'] for s in spec['sources']}
    stored = service.store.sources(task['session_id'], task['project_id'], list(declared))
    if {s['source_id']: s['text'] for s in stored} != declared or any(s['text_sha256'] != hashlib.sha256(s['text'].encode()).hexdigest() for s in stored):
        raise ValueError('saved assessment trusted sources drift')
    base = inside_week12(Path(service.project_root) / 'projects' / task['project_id'] / 'versions' / task['version_id'])
    result = task.get('result') or {};path = base / 'artifact-manifest.json'
    declared_path = result.get('outcome_manifest_path')
    if not isinstance(declared_path, str) or Path(declared_path).resolve() != path.resolve():
        raise ValueError('saved assessment manifest path differs')
    _no_links(path)
    # Reject link aliases before hashing any file named by this exact manifest.
    raw_manifest = json.loads(path.read_bytes())
    for name in raw_manifest.get('files', {}):
        if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('unsafe saved assessment evidence path')
        _no_links(base / name)
    manifest = _verify_manifest(path, result.get('outcome_manifest_sha256'), base)
    expected = dict(task_id=task['task_id'], project_id=task['project_id'], spec_digest=proposal['spec_digest'], source_sha256=display['source_sha256'],
                    owner=task['worker_owner'], context_id=display['execution_context_id'], context_sha256=display['execution_context_sha256'])
    if proposal['operation'] == 'modify':expected['artifact_baseline'] = spec['baseline_ref']
    if manifest.get('schema') != 'v11-artifact-manifest-1' or manifest.get('binding') != expected:
        raise ValueError('saved assessment manifest identity differs')
    outcome = manifest['outcome']
    committed = {k: v for k, v in result.items() if k not in RESULT_META - {'saved_assessment'}}
    if committed != outcome or json.loads((base / 'outcome.json').read_bytes()) != outcome:
        raise ValueError('saved assessment outcome/result drift')
    if json.loads((base / 'spec.json').read_bytes()) != spec or json.loads((base / 'confirmation.json').read_bytes()) != confirmation:
        raise ValueError('saved assessment frozen spec/confirmation differs')
    if (base / 'frozen-model.mo').read_bytes() != source.encode() or (base / 'frozen-model.sysml').read_bytes() != spec['artifact'].get('sysml_source', '').encode():
        raise ValueError('saved assessment frozen source differs')
    receipt = outcome.get('saved_assessment')
    if not isinstance(receipt, dict) or receipt.get('schema') != SCHEMA:
        raise ValueError('no saved fixed-assessment binding; explicit full recheck required')
    if receipt.get('task') != {k: task.get(k) for k in TASK_KEYS} or any(receipt.get(k) != ctx.get(k) for k in ('request_id', 'request_digest', 'displayed_digest', 'baseline_ref')):
        raise ValueError('saved assessment request/task binding differs')
    if receipt.get('spec_digest') != proposal['spec_digest'] or receipt.get('source_sha256') != display['source_sha256'] or receipt.get('assessment_sha256') != digest_json(_payload(outcome)):
        raise ValueError('saved assessment content binding differs')
    evidence = {name: row['sha256'] for name, row in manifest['files'].items() if name != 'outcome.json'}
    if receipt.get('evidence_sha256') != evidence:
        raise ValueError('saved assessment evidence content binding differs')
    checked = receipt.get('checked_input_sha256')
    if not isinstance(checked, dict) or not checked or any(evidence.get(name) != value for name, value in checked.items()):
        raise ValueError('saved assessment checked input content binding differs')
    if receipt.get('checker') != checker_binding(spec):
        raise ValueError('checker rule/source changed; explicit full recheck required')
    return spec, outcome, base


def _projection(task, spec, outcome, base):
    from .case_contact_review import SCHEMA as REVIEW_SCHEMA, IDENTITY_FIELDS, current_summary, mission_summary
    from .case_scope import modelica_parameters
    identity = dict(project_id=task['project_id'], task_id=task['task_id'], version_id=task['version_id'],
                    spec_digest=outcome['saved_assessment']['spec_digest'], source_sha256=outcome['saved_assessment']['source_sha256'],
                    manifest_sha256=task['result']['outcome_manifest_sha256'])
    mission = deepcopy(outcome.get('mission_assessment') or {})
    # Preserve the final combined checker result, including parameter/tool blocks.
    mission.update(task_status=outcome.get('task_status', 'UNKNOWN'), completion_status=outcome.get('completion_status', mission.get('completion_status', 'UNKNOWN')))
    exited = task['state'] in ('succeeded', 'failed') and task['result'].get('worker_exit_code') == 0
    execution_ok = exited and outcome.get('tool_status') == 'PASS' and outcome.get('chain_status') == 'PASS' and not outcome.get('test_only')
    if not execution_ok:mission.update(task_status='UNKNOWN', completion_status='TOOL_EXECUTION_INCOMPLETE' if outcome.get('tool_status') != 'PASS' else 'EXECUTION_EVIDENCE_INCOMPLETE')
    checker = outcome['saved_assessment']['checker']
    review = dict(schema=REVIEW_SCHEMA, rule_version=checker['rule_version'], status=mission.get('validity_status', 'UNKNOWN'),
                  validity_status=mission.get('validity_status', 'UNKNOWN'), task_status=mission.get('task_status', 'UNKNOWN'),
                  completion_status=mission.get('completion_status', 'UNKNOWN'), route_status=mission.get('route_status', 'UNKNOWN'),
                  diagnostic_status=mission.get('diagnostic_status', 'UNKNOWN'), overall_summary=mission.get('overall_summary', 'EVIDENCE_UNKNOWN'),
                  current_assessment=mission_summary(mission), execution_evidence_status='PASS' if execution_ok else 'UNKNOWN',
                  current_chain_status=outcome.get('chain_status', 'UNKNOWN'), issues=mission.get('issues', []), physical_status='UNKNOWN',
                  saved_assessment_status='CONTENT_AND_CHECKER_VERIFIED_REUSED',
                  binding=dict(identity, csv_sha256=outcome['saved_assessment']['evidence_sha256'].get('tool/model_res.csv'), **checker))
    record = dict(identity, record_kind='SAVED_ASSESSMENT_CONTENT_VERIFIED', state=task['state'], evidence_status='COMPLETE' if exited else 'INCOMPLETE',
                  tool_status=outcome.get('tool_status', 'UNKNOWN'), chain_status=outcome.get('chain_status', 'UNKNOWN'),
                  task_status=outcome.get('task_status', 'UNKNOWN'), test_only=outcome.get('test_only', False), current_contact_review=review)
    summary = current_summary(record)
    return dict(summary, parameters=modelica_parameters(spec['artifact']['modelica_source'])[0], mission=mission_summary(mission),
                runtime_parameter_readback=deepcopy(outcome.get('runtime_parameter_readback')), physical_acceptance_status='NOT_VERIFIED',
                saved_assessment_status='CONTENT_AND_CHECKER_VERIFIED_REUSED', outcome_manifest_path=str(base / 'artifact-manifest.json'))


def concise_result(flow):
    flow.refresh();state = flow.state();ctx = flow.ctx
    review_only = ctx.get('access_mode') == 'RESULT_REVIEW_ONLY'
    latest = state.get('reviewed_execution') if review_only else state.get('latest_request_execution')
    result = dict(schema='v12-concise-saved-result-1', request_id=ctx['request_id'], reused=True, state=deepcopy(state),
                  product_http_added=0, execution_added=0, physics_checker_invocations=0, explanation_body_checked=False,
                  explanation=dict(status='NOT_CHECKED_USE_RESULT_FULL'), stage='NO_TERMINAL_EXECUTION')
    if not latest:return result
    svc = flow.open(read_only=True);task = svc.store.task(latest['task_id'])
    if any(task.get(k) != latest.get(k) for k in ('project_id', 'task_id', 'version_id', 'state')):
        raise ValueError('concise result current task/index differs')
    try:
        if task['state'] not in ('succeeded', 'failed', 'cancelled', 'uncertain'):
            raise ValueError('task still active; waiting for final bound assessment')
        # A review-only context is a different request. Do not silently reuse an
        # origin receipt under this identity; full explicitly checks that scope.
        spec, outcome, base = verify_saved(svc, task, ctx)
        execution = _projection(task, spec, outcome, base)
        result.update(stage='SAVED_FIXED_ASSESSMENT_REUSED', execution=execution, saved_assessment_status='CONTENT_AND_CHECKER_VERIFIED_REUSED')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        execution = deepcopy(latest)
        execution.update(record_kind='SAVED_ASSESSMENT_RECHECK_REQUIRED', tool_status='UNKNOWN', chain_status='NEEDS_RECHECK', task_status='UNKNOWN',
                         saved_assessment_status='NEEDS_RECHECK', current_contact_review=dict(status='UNKNOWN', review_state='EVIDENCE_INSUFFICIENT', issues=[str(exc)]))
        if (task.get('result') or {}).get('test_only') is True:execution['test_only'] = True
        result.update(stage='SAVED_ASSESSMENT_NEEDS_RECHECK', execution=execution, saved_assessment_status='NEEDS_RECHECK', recheck_reason=str(exc))
    state_key = 'reviewed_execution' if review_only else 'latest_request_execution'
    result['state'][state_key] = deepcopy(execution)
    return result
