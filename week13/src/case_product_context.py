"""Lossless current-model projection; historical evidence stays in the Store.

Only the exact bootstrap-owned historical envelope is unpacked. Ordinary source
text is never summarized. Hashes identify omitted historical models/evidence;
exact_quotes are verified substrings, never replacement source text.
"""
from copy import deepcopy
import hashlib
import json

from .case_paths import sha_value

PREFIX = '所选历史基线证据（不是本次新要求）：\n'
SCHEMA = 'v12-product-context-projection-1'


def text_binding(text):
    return dict(text_sha256=hashlib.sha256(text.encode('utf8')).hexdigest(), text_utf8_bytes=len(text.encode('utf8')))


def historical_envelope(text):
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    try:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate envelope key')
                value[key] = item
            return value
        value = json.loads(text[len(PREFIX):], object_pairs_hook=unique)
    except (ValueError, TypeError):
        return None
    old = {'reference', 'spec', 'task_status', 'limitations'}
    new = old | {'schema', 'spec_sha256', 'source_bindings', 'source_history'}
    if not isinstance(value, dict) or set(value) not in (old, new):
        return None
    if set(value) == new and value.get('schema') != 'v12-historical-baseline-source-2':
        return None
    spec, ref = value.get('spec'), value.get('reference')
    if not isinstance(spec, dict) or not isinstance(ref, dict) or not isinstance(spec.get('artifact', {}).get('modelica_source'), str):
        return None
    if set(value) == old and sha_value(spec) != ref.get('spec_digest'):
        return None
    if set(value) == new and (value.get('spec_sha256') != ref.get('spec_digest') or 'sources' in spec):
        return None
    return value


def bootstrap_source(snapshot, reference):
    """Future source retains direct facts, but never embeds recursive sources."""
    spec = snapshot['frozen_spec']
    projection = Projection(dict(baseline_snapshot=snapshot, sources=[]))
    projection.finish_quotes()
    value = dict(schema='v12-historical-baseline-source-2', reference=reference,
                 spec_sha256=sha_value(spec), spec={k: deepcopy(v) for k, v in spec.items() if k != 'sources'},
                 source_bindings=list(projection.catalog.values()), source_history=list(projection.history.values()),
                 task_status=snapshot.get('task_status'), limitations='简化模型证据，不是硬件认证')
    return PREFIX + json.dumps(value, ensure_ascii=False)


def outcome_summary(outcome):
    """Keep result facts/readbacks; replace event lists and logs with bindings."""
    value = deepcopy(outcome)
    tool = outcome.get('tool_result')
    if isinstance(tool, dict):
        value['tool_result_summary'] = {k: deepcopy(tool[k]) for k in
            ('actual_run_settings', 'solver', 'tool', 'model_name', 'stop', 'intervals', 'numeric_tolerance') if k in tool}
        for name in ('compile', 'simulation'):
            item = tool.get(name)
            if isinstance(item, dict):
                value['tool_result_summary'][name] = {k: deepcopy(item[k]) for k in
                    ('status', 'returncode', 'success', 'duration', 'duration_s', 'elapsed_s', 'error') if k in item}
        if 'runtime_parameter_readback' in tool and tool['runtime_parameter_readback'] != outcome.get('runtime_parameter_readback'):
            value['tool_result_summary']['runtime_parameter_readback'] = deepcopy(tool['runtime_parameter_readback'])
    for key in ('tool_result', 'stdout', 'stderr', 'logs'):
        if key in value:
            item = value.pop(key)
            value[key + '_binding'] = dict(sha256=sha_value(item), role='HISTORICAL_TOOL_EVIDENCE_NOT_RELOADED')
    mission = value.get('mission_assessment')
    if isinstance(mission, dict):
        for key in ('events', 'terrain_events'):
            if key in mission:
                item = mission.pop(key)
                mission[key + '_binding'] = dict(sha256=sha_value(item), count=len(item))
    return value


class Projection:
    def __init__(self, ctx):
        self.ctx = ctx
        self.catalog = {}
        self.history = {}
        self.quotes = {}
        self.literal_ids = {ctx['sources'][0]['id']} if ctx['sources'] else set()
        self._requirements(ctx['baseline_snapshot']['frozen_spec'])
        self._spec_sources(ctx['baseline_snapshot']['frozen_spec'])
        for source in ctx['sources']:
            self.source(source)

    def _requirements(self, spec):
        for row in spec.get('requirements', []):
            if isinstance(row, dict) and isinstance(row.get('source_text'), str):
                self.quotes.setdefault(row.get('source_id'), set()).add(row['source_text'])

    def _spec_sources(self, spec):
        if spec.get('sources'):
            self.literal_ids.add(spec['sources'][0]['id'])
        for source in spec.get('sources', []):
            self.source(source)

    def source(self, source):
        sid, text = source['id'], source['text']
        binding = dict(id=sid, **text_binding(text))
        if sid in self.catalog:
            if any(self.catalog[sid][k] != v for k, v in binding.items()):
                raise ValueError('historical source ID/text binding conflict')
            return binding
        envelope = None if sid in self.literal_ids else historical_envelope(text)
        if envelope is None:
            self.catalog[sid] = dict(binding, text=text, role='ORIGINAL_SOURCE_TEXT')
        else:
            self.catalog[sid] = dict(binding, role='HISTORICAL_BASELINE_ENVELOPE_NOT_NEW_REQUIREMENT',
                                     original_text_ref=dict(kind='TRUSTED_STORE_SOURCE_ID', source_id=sid),
                                     reference=envelope['reference'], exact_quotes=[PREFIX])
            spec = envelope['spec']
            self._requirements(spec)
            self._spec_sources(spec)
            # New envelopes carry a flat provenance catalog, so original user
            # constraints survive future generations without recursive JSON.
            for ancestor in envelope.get('source_bindings', []):
                if 'text' in ancestor:
                    if text_binding(ancestor['text']) != {k: ancestor[k] for k in ('text_sha256', 'text_utf8_bytes')}:
                        raise ValueError('flat historical source text binding drift')
                    self.literal_ids.add(ancestor['id'])
                    self.source(ancestor)
                elif ancestor['id'] not in self.catalog:
                    self.catalog[ancestor['id']] = deepcopy(ancestor)
            for facts in envelope.get('source_history', []):
                self.history[sha_value(facts['reference'])] = deepcopy(facts)
            artifact = spec['artifact']
            self.history[sha_value(envelope['reference'])] = dict(
                reference=deepcopy(envelope['reference']), task_status=envelope['task_status'], limitations=envelope['limitations'],
                role='HISTORICAL_FACTS_NOT_NEW_REQUIREMENTS',
                spec_facts={k: deepcopy(v) for k, v in spec.items() if k not in ('sources', 'artifact')},
                artifact_binding=dict(model_name=artifact.get('model_name'), sha256=sha_value(artifact)),
                source_bindings=[dict(id=s['id'], **text_binding(s['text'])) for s in spec.get('sources', [])]
                    if 'sources' in spec else deepcopy(envelope.get('source_bindings', [])))
        return binding

    def finish_quotes(self):
        # Every quote is checked against the original text, including inherited
        # baseline requirements needed for product-generated baseline SysML.
        originals = {}
        def collect(spec):
            for source in spec.get('sources', []):
                originals[source['id']] = source['text']
                envelope = None if source['id'] in self.literal_ids else historical_envelope(source['text'])
                if envelope:
                    collect(envelope['spec'])
                    for ancestor in envelope.get('source_bindings', []):
                        if 'text' in ancestor:
                            originals[ancestor['id']] = ancestor['text']
        collect(self.ctx['baseline_snapshot']['frozen_spec'])
        originals.update({s['id']: s['text'] for s in self.ctx['sources']})
        for sid, quotes in self.quotes.items():
            if sid not in self.catalog:
                # A v2 source binding may intentionally refer to Store-only
                # ancestors; the current frozen spec still carries its sources.
                continue
            for quote in sorted(quotes):
                if sid not in originals:
                    # Previously verified excerpts remain bound to the original
                    # source hash; they are not promoted into replacement text.
                    if quote and quote not in self.catalog[sid].get('exact_quotes', []):
                        raise ValueError('historical quote not present in bound flat source excerpts')
                    continue
                if quote and quote not in originals[sid]:
                    raise ValueError('baseline requirement quote differs from original source')
                if quote and 'exact_quotes' in self.catalog[sid] and quote not in self.catalog[sid]['exact_quotes']:
                    self.catalog[sid]['exact_quotes'].append(quote)

    def snapshot(self):
        original = self.ctx['baseline_snapshot']
        value = deepcopy({k: v for k, v in original.items() if k not in ('frozen_spec', 'outcome')})
        spec = deepcopy({k: v for k, v in original['frozen_spec'].items() if k != 'sources'})
        spec['sources'] = [self.source(s) for s in original['frozen_spec'].get('sources', [])]
        value['frozen_spec'] = spec
        if 'outcome' in original:
            value['outcome'] = outcome_summary(original['outcome'])
        value['projection_binding'] = dict(schema=SCHEMA, original_snapshot_sha256=sha_value(original),
                                           original_frozen_spec_sha256=sha_value(original['frozen_spec']))
        return value


def project_context(ctx, context):
    value = deepcopy(context)
    # Already projected input must be validated by the stage journal reader;
    # do not project or recursively summarize it a second time.
    if 'product_context_projection' in value:
        return value
    projection = Projection(ctx)
    from .case_contact_review import review_snapshot,rule_for_spec
    if ctx.get('project_root'):
        value['baseline_current_contact_review']=review_snapshot(ctx['baseline_snapshot'],ctx['project_root'])
        value['current_contact_review_precedence']=rule_for_spec(ctx['baseline_snapshot']['frozen_spec'])
    if value.get('baseline_snapshot') == ctx['baseline_snapshot']:
        value['baseline_snapshot'] = projection.snapshot()
    if value.get('trusted_sources') == ctx['sources']:
        value['trusted_sources'] = [projection.source(s) for s in ctx['sources']]
    for key in ('confirmed_plan', 'trusted_generation_contract'):
        if isinstance(value.get(key), dict):
            projection._requirements(value[key])
    actual = value.get('actual_request_execution')
    if isinstance(actual, dict) and isinstance(actual.get('outcome'), dict):
        actual['outcome'] = outcome_summary(actual['outcome'])
    projection.finish_quotes()
    value['product_context_projection'] = dict(
        schema=SCHEMA, request_digest=ctx['request_digest'], baseline_ref=ctx['baseline_ref'],
        original_baseline_snapshot_sha256=sha_value(ctx['baseline_snapshot']),
        original_sources_sha256=sha_value(ctx['sources']),
        source_catalog=list(projection.catalog.values()), historical_baselines=list(projection.history.values()),
        citation_rule='Only text or exact_quotes are verbatim source text. Hashes, spec_facts and summaries are NOT source text; never cite them as original. Historical facts are not new requirements. Full original sources remain in the trusted Store; candidate sources return ordered IDs only.')
    return value


def project_messages(ctx, messages):
    result = deepcopy(messages)
    if len(result) > 1 and result[1].get('role') == 'user':
        try:
            context = json.loads(result[1]['content'])
        except (ValueError, TypeError):
            return result
        if isinstance(context, dict) and any(k in context for k in ('baseline_snapshot', 'bound_existing_execution')):
            result[1]['content'] = json.dumps(project_context(ctx, context), ensure_ascii=False)
    return result


def projected_context_matches(ctx, context):
    """Validate projected source facts for existing strict recovery adapters."""
    if 'product_context_projection' not in context:
        return context.get('baseline_snapshot') == ctx['baseline_snapshot'] and context.get('trusted_sources', ctx['sources']) == ctx['sources']
    raw = deepcopy(context)
    raw.pop('product_context_projection')
    raw['baseline_snapshot'] = ctx['baseline_snapshot']
    if 'trusted_sources' in raw:
        raw['trusted_sources'] = ctx['sources']
    return project_context(ctx, raw) == context
