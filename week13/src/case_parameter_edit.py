"""Apply a confirmed product plan to a verified finite Asym2 baseline.

This is a mechanical source-editing tool, never a product-model response or a
planner. The ordinary candidate/delta/parser/MCP gates remain the caller's job.
"""
from copy import deepcopy
import json
import re

from .case_asym2_family import (
    FAMILY, _replace, is_asym2_source, modelica_parameters, sysml_parameters,
)
from .case_paths import sha_file, sha_value
from .case_scope import MASS_SCOPE_PACKET, STAIR_SCOPE_PACKET, validate_request_plan

ORIGIN = 'TOOL_APPLIED_CONFIRMED_PRODUCT_PLAN'


def edit_mode(ctx):
    """Select only the two thin-tool paths; retain all older generation paths."""
    snapshot = ctx['baseline_snapshot']; base = snapshot['frozen_spec']
    source = base['artifact'].get('modelica_source', '')
    if (not is_asym2_source(source) or not base['artifact'].get('sysml_source')
            or snapshot.get('recorded_conclusions_verified') is not True
            or snapshot.get('evidence_status') != 'COMPLETE'):
        return None
    plan = ctx['plan']; proposed = plan.get('proposed_parameters', {})
    if plan.get('scope') == MASS_SCOPE_PACKET:
        return 'm'
    before = modelica_parameters(source)[0]
    if plan.get('scope') == STAIR_SCOPE_PACKET and proposed.get('stepD') == before['stepD']:
        return 'stepH'
    return None


def _edit_sysml(source, parameter, expression, requirements, model_name):
    """Use the existing parser's source spans, preserving all other body text."""
    from sysmlpy.antlr_parser import parse
    from .inherited.artifact_sysml import _nodes
    tree = parse(source)
    systems = [node for node in _nodes(tree, 'PartDefinitionContext')
               if node.getText().startswith('partdefSystem{')]
    if len(systems) != 1:
        raise ValueError('parameter tool requires one verified SysML System')
    attributes = [node for node in _nodes(systems[0], 'AttributeUsageContext')
                  if re.match(r'attribute' + re.escape(parameter) + r':', node.getText())]
    if len(attributes) != 1:
        raise ValueError('parameter tool requires a unique SysML mirror: ' + parameter)
    node = attributes[0]
    kind = re.fullmatch(r'attribute\w+:(Real|Integer)=.*;', node.getText())[1]
    edits = [(node.start.start, node.stop.stop + 1,
              'attribute ' + parameter + ' : ' + kind + ' = ' + expression + ';')]
    old = sorted(_nodes(tree, 'RequirementUsageContext'), key=lambda node: node.start.start)
    if not old:
        raise ValueError('parameter tool requires verified baseline requirement traces')
    rendered = []
    for index, row in enumerate(requirements, 1):
        fields = dict(row, model_name=model_name)
        body = '\n'.join('    attribute ' + key + ' : String = ' + json.dumps(fields[key], ensure_ascii=False) + ';'
                         for key in ('text', 'source_id', 'source_text', 'model_name'))
        rendered.append('requirement req_' + str(index) + ' {\n' + body + '\n  }')
    for index, node in enumerate(old):
        edits.append((node.start.start, node.stop.stop + 1, '\n  '.join(rendered) if index == 0 else ''))
    for start, end, value in sorted(edits, reverse=True):
        source = source[:start] + value + source[end:]
    return source


def apply_confirmed_plan(ctx):
    """Return a complete candidate plus explicit non-model provenance."""
    from .inherited.model_artifact import validate_artifact
    mode = edit_mode(ctx)
    if mode is None:
        raise ValueError('request is outside verified Asym2 m/stepH parameter-tool paths')
    snapshot = ctx['baseline_snapshot']; base = snapshot['frozen_spec']; plan = ctx['plan']
    evidence = validate_request_plan(plan, base, ctx['sources'], task_context=ctx)
    checked = validate_artifact(base, require_sysml=True)
    if (not checked['ready'] or checked['spec_digest'] != snapshot.get('spec_digest')
            or checked['source_sha256'] != snapshot.get('source_sha256')):
        raise ValueError('parameter tool baseline dual-source/digest validation failed')
    if not evidence['executable']:
        raise ValueError('parameter tool requires an executable confirmed product plan')
    before, expressions, _ = modelica_parameters(base['artifact']['modelica_source'])
    spec = deepcopy(base)
    source = _replace(base['artifact']['modelica_source'], mode, plan['proposed_parameters'][mode])
    after, new_expressions, _ = modelica_parameters(source)
    if any(new_expressions[name] != value for name, value in expressions.items() if name != mode):
        raise ValueError('parameter tool changed a frozen expression')
    mirrors = sysml_parameters(source)
    spec['artifact']['modelica_source'] = source
    spec['artifact']['sysml_source'] = _edit_sysml(base['artifact']['sysml_source'], mode,
        mirrors[1][mode], plan['requirements'], base['artifact']['model_name'])
    spec.update(baseline_ref=deepcopy(ctx['baseline_ref']), sources=deepcopy(ctx['sources']),
                requirements=deepcopy(plan['requirements']), design=deepcopy(plan['design']))
    audit = dict(schema='v12-confirmed-parameter-tool-1', origin=ORIGIN, family=FAMILY,
        tool='case_parameter_edit.apply_confirmed_plan', tool_source_sha256=sha_file(__file__),
        request_id=ctx['request_id'], request_digest=ctx['request_digest'],
        displayed_digest=ctx['displayed_digest'], baseline_ref=deepcopy(ctx['baseline_ref']),
        baseline_snapshot_sha256=sha_value(snapshot), baseline_spec_digest=checked['spec_digest'],
        baseline_source_sha256=checked['source_sha256'], confirmed_product_plan_sha256=sha_value(plan),
        applied_direct_parameter=mode, before=before[mode], after=after[mode],
        selected_baseline_mass=before['m'], total_mass=after['m'], added_point_load=after['m']-before['m'],
        candidate_spec_digest=sha_value(spec), candidate_source_sha256=__import__('hashlib').sha256(source.encode()).hexdigest(),
        candidate_sysml_sha256=__import__('hashlib').sha256(spec['artifact']['sysml_source'].encode()).hexdigest(),
        requirements_sha256=sha_value(plan['requirements']), sources_sha256=sha_value(ctx['sources']),
        other_modelica_text_unchanged=True, derived_expressions_unchanged=True,
        product_generated_source=False, product_http_added=0, execution_added=0)
    return spec, audit
