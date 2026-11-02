"""DeepSeek strict-Beta wire adapter; business schemas remain unchanged.

Only closed, context-known objects are accepted. Unsupported provider keywords
stay in the internal schema and are checked locally. One explicitly audited
generation envelope suffix is recognized without editing any JSON value.
Official basis: https://api-docs.deepseek.com/zh-cn/guides/tool_calls/
"""
from copy import deepcopy
import json
import math

STRICT_PROFILE = 'deepseek-strict-beta-v1'
FLAT_PLAN_PROFILE = 'deepseek-strict-beta-plan-flat-v1'
STRICT_PROFILES = (STRICT_PROFILE, FLAT_PLAN_PROFILE)
LEGACY_PROFILE = 'deepseek-tools-legacy-v1'
STRICT_BASE = 'https://api.deepseek.com/beta'
LEGACY_BASE = 'https://api.deepseek.com'
EMPTY_MARKER = {'v12_empty_object': 'EMPTY_OBJECT'}
EMPTY_GUIDE = '\nStrict transport encoding only: at schema-declared empty-object branches use {"v12_empty_object":"EMPTY_OBJECT"}. The host restores exactly {} after validating the wire schema. This marker adds no requirement, design value, permission or physical change. All nonempty branches and all strings/numbers remain unchanged.'
_LOCAL_ONLY = {'minLength', 'maxLength', 'minItems', 'maxItems', 'uniqueItems', 'minProperties', 'maxProperties'}
_WIRE_KEYS = {'type', 'description', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'anyOf',
              'pattern', 'format', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf', 'const', 'default'}


def transport_profile(config):
    profile = config.get('tool_output_profile', LEGACY_PROFILE)
    if profile not in (LEGACY_PROFILE,) + STRICT_PROFILES:
        raise ValueError('unknown tool output profile')
    expected = STRICT_BASE if profile in STRICT_PROFILES else LEGACY_BASE
    if config.get('api_base') != expected:
        raise ValueError('tool output profile and exact API base differ')
    return dict(profile=profile, api_base=expected, endpoint=expected + '/chat/completions',
                strict_tools=profile in STRICT_PROFILES, provider_echo_claimed=False)


_FLAT_DESIGN = dict(summary='design_summary', assumptions='design_assumptions', parameters='design_parameters')


def flat_plan_schema(schema):
    """Remove wrappers only; all original business fields remain represented."""
    if set(schema.get('properties', {})) != {'packet'}:
        raise ValueError('flat PLAN requires the original packet schema')
    packet = schema['properties']['packet']
    props = deepcopy(packet['properties'])
    branches = props.pop('design').get('oneOf', [])
    if len(branches) != 2 or set(branches[0].get('properties', {})) != set(_FLAT_DESIGN) or not _empty_schema(branches[1]):
        raise ValueError('flat PLAN requires the declared full/empty design branches')
    if set(packet.get('required', [])) != set(packet['properties']):
        raise ValueError('flat PLAN cannot omit optional business fields')
    props['design_state'] = dict(type='string', enum=['EMPTY', 'OBJECT'])
    for source, target in _FLAT_DESIGN.items():
        if target in props: raise ValueError('flat PLAN field collision')
        props[target] = deepcopy(branches[0]['properties'][source])
    # EMPTY has an explicit canonical representation, checked before mapping.
    props['design_summary'].pop('minLength', None)
    return dict(type='object',properties=props,required=list(props),additionalProperties=False)


def plan_transport_guide(tool):
    schema = flat_plan_schema(tool['function']['parameters'])
    return ('浅层PLAN运输格式：arguments直接包含全部字段 '+', '.join(schema['required'])+
        '。禁止packet或design对象包装。design_state=OBJECT时用design_summary、design_assumptions、design_parameters表达原设计；'
        '原design为空时design_state=EMPTY且design_summary=""、design_assumptions=[]、design_parameters=[]。其余字段原样，包括完整proposed_parameters和scope；'
        '所有原业务约束、来源和数值仍适用。缺项/不支持不能伪造方案。不得返回嵌套包装、JSON字符串或不完整JSON。')


def _unflatten_plan(value):
    packet = {key: deepcopy(item) for key,item in value.items() if key not in {'design_state', *_FLAT_DESIGN.values()}}
    if value['design_state'] == 'EMPTY':
        if value['design_summary'] != '' or value['design_assumptions'] != [] or value['design_parameters'] != []:
            raise ValueError('flat EMPTY design requires exact canonical empty fields')
        packet['design'] = deepcopy(EMPTY_MARKER)
    else:
        packet['design'] = {key: deepcopy(value[target]) for key,target in _FLAT_DESIGN.items()}
    return dict(packet=packet)


def _disjoint_objects(branches):
    """Prove oneOf=>anyOf for closed objects by keys or disjoint enums."""
    for branch in branches:
        if branch.get('type') != 'object' or not (branch.get('additionalProperties') is False or branch.get('maxProperties') == 0 and not branch.get('properties')):
            return False
        props = branch.get('properties', {})
        if set(branch.get('required', [])) != set(props):
            return False
    for index, left in enumerate(branches):
        for right in branches[index + 1:]:
            lp, rp = left.get('properties', {}), right.get('properties', {})
            if set(lp) != set(rp):
                continue
            if not any('enum' in lp[key] and 'enum' in rp[key] and
                       not any(value in rp[key]['enum'] for value in lp[key]['enum']) for key in lp):
                return False
    return True


def frozen_value_schema(value):
    """Expose a known contract's structure without inventing missing values."""
    if isinstance(value, dict):
        return dict(type='object', properties={key: frozen_value_schema(item) for key, item in value.items()},
                    required=list(value), additionalProperties=False, enum=[deepcopy(value)])
    if isinstance(value, list):
        branches = []
        for item in value:
            branch = frozen_value_schema(item)
            if branch not in branches:
                branches.append(branch)
        # Empty frozen lists can only accept [] locally. The wire item schema
        # remains typed; the local enum is authoritative for exact identity.
        items = branches[0] if len(branches) == 1 else dict(anyOf=branches) if branches else dict(type='string')
        return dict(type='array', items=items, enum=[deepcopy(value)])
    if type(value) is bool:
        return dict(type='boolean', enum=[value])
    if type(value) in (int, float) and math.isfinite(value):
        return dict(type='integer' if type(value) is int else 'number', enum=[value])
    if isinstance(value, str):
        return dict(type='string', enum=[value])
    raise ValueError('frozen strict contract contains unsupported/null/nonfinite value')


def _empty_schema(schema):
    return schema.get('type')=='object' and not schema.get('properties') and (schema.get('maxProperties')==0 or schema.get('properties')=={} and schema.get('required')==[] and schema.get('additionalProperties') is False)


def wire_schema(schema, path='$', removed=None):
    if not isinstance(schema, dict):
        raise ValueError('strict schema must be an object: ' + path)
    result = deepcopy(schema)
    if _empty_schema(result):
        if removed is not None:removed.append(dict(path=path,transformation='DECLARED_EMPTY_OBJECT_TO_CLOSED_TRANSPORT_MARKER',internal_value={},wire_value=deepcopy(EMPTY_MARKER)))
        return dict(type='object',properties=dict(v12_empty_object=dict(type='string',enum=['EMPTY_OBJECT'])),required=['v12_empty_object'],additionalProperties=False)
    if result.get('type') == 'object' and 'properties' not in result and result.get('maxProperties') != 0:
        raise ValueError('unresolved object shape requires a context-known contract: ' + path)
    if 'oneOf' in result:
        branches = result.pop('oneOf')
        if 'anyOf' in result or not isinstance(branches, list) or not branches or not _disjoint_objects(branches):
            raise ValueError('oneOf branches lack a proof of equivalence to anyOf: ' + path)
        result['anyOf'] = branches
    for key in _LOCAL_ONLY:
        if key in result:
            if removed is not None:
                removed.append(dict(path=path, keyword=key, value=deepcopy(result[key]), enforced='LOCAL_INTERNAL_SCHEMA'))
            result.pop(key)
    if 'enum' in result and any(isinstance(item, (dict, list)) for item in result['enum']):
        if removed is not None:
            removed.append(dict(path=path, keyword='enum', value=deepcopy(result['enum']), enforced='LOCAL_EXACT_FROZEN_CONTRACT'))
        result.pop('enum')
    unknown = set(result) - _WIRE_KEYS
    if unknown:
        raise ValueError('unsupported strict schema keyword at ' + path + ': ' + ','.join(sorted(unknown)))
    if 'anyOf' in result:
        if not isinstance(result['anyOf'], list) or not result['anyOf']:
            raise ValueError('strict anyOf requires branches: ' + path)
        result['anyOf'] = [wire_schema(item, path + '.anyOf[' + str(i) + ']', removed) for i, item in enumerate(result['anyOf'])]
    kind = result.get('type')
    if kind is None and 'anyOf' not in result or kind is not None and kind not in ('object', 'string', 'number', 'integer', 'boolean', 'array'):
        raise ValueError('unsupported or missing strict schema type: ' + path)
    if kind == 'object':
        props = result.get('properties', {})
        if not isinstance(props, dict) or result.get('additionalProperties', False) is not False:
            raise ValueError('open/dynamic strict object requires a context-known contract: ' + path)
        if set(result.get('required', [])) != set(props):
            raise ValueError('optional object fields must be resolved from trusted context: ' + path)
        result.update(properties={key: wire_schema(item, path + '.' + key, removed) for key, item in props.items()},
                      required=list(props), additionalProperties=False)
    if kind == 'array':
        if 'items' not in result:
            raise ValueError('strict array items required: ' + path)
        result['items'] = wire_schema(result['items'], path + '[]', removed)
    return result


def generation_tool(base_tool, contract, *, needs_baseline):
    """Specialize only contract-known fields; source strings remain generated."""
    tool = deepcopy(base_tool)
    arguments = tool['function']['parameters']
    spec = arguments['properties']['spec']
    outputs = contract.get('outputs')
    if not isinstance(outputs, dict) or not outputs or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in outputs.items()):
        raise ValueError('strict generation requires complete frozen output names/expressions')
    spec['properties']['outputs'] = frozen_value_schema(outputs)
    if 'trajectory_contract' in contract:
        spec['properties']['trajectory_contract'] = frozen_value_schema(contract['trajectory_contract'])
        if 'trajectory_contract' not in spec['required']:
            spec['required'].append('trajectory_contract')
    else:
        spec['properties'].pop('trajectory_contract', None)
        spec['required'] = [key for key in spec['required'] if key != 'trajectory_contract']
    if needs_baseline:
        arguments['properties']['baseline_sysml_source'] = dict(type='string', minLength=1)
        if 'baseline_sysml_source' not in arguments['required']:
            arguments['required'].append('baseline_sysml_source')
    else:
        arguments['properties'].pop('baseline_sysml_source', None)
        arguments['required'] = [key for key in arguments['required'] if key != 'baseline_sysml_source']
    return tool


def prepare_tools(tools, *, profile=STRICT_PROFILE):
    if profile not in STRICT_PROFILES: raise ValueError('strict preparation profile required')
    prepared, audit = [], []
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {'type', 'function'} or tool.get('type') != 'function':
            raise ValueError('strict mode only accepts function tools')
        value = deepcopy(tool)
        function = value['function']
        if not isinstance(function, dict) or not isinstance(function.get('name'), str) or not function['name']:
            raise ValueError('strict function name required')
        if set(function) - {'name', 'description', 'parameters', 'strict'}:
            raise ValueError('unsupported strict function fields')
        removed = []
        if profile == FLAT_PLAN_PROFILE and function['name'] == 'case_request_plan':
            parameters = function['parameters']
            if 'packet' in parameters.get('properties', {}):
                function['parameters'] = flat_plan_schema(parameters)
                removed.append(dict(transformation='PLAN_PACKET_DESIGN_TO_FLAT_FIELDS',profile=profile))
            elif not {'design_state', *_FLAT_DESIGN.values()}.issubset(parameters.get('properties', {})):
                raise ValueError('unrecognized flat PLAN schema; no implicit conversion')
        function['parameters'] = wire_schema(function['parameters'], removed=removed)
        function['strict'] = True
        prepared.append(value)
        audit.append(dict(function=function['name'], local_only_constraints=removed))
    return prepared, audit


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = item
        return result
    def invalid(value):
        raise ValueError('nonfinite JSON number: ' + value)
    value = json.loads(text, object_pairs_hook=unique, parse_constant=invalid)
    json.dumps(value, allow_nan=False)
    return value


def generation_suffix_calls(calls, finish_reason):
    """Remove exactly one surplus final } from a complete proposal envelope.

    The complete object is decoded with unique keys and finite numbers first.
    Source strings and every business value survive unchanged. This helper
    never handles fragments, other tools, multiple calls, or guessed closings.
    Ordinary valid calls are returned untouched; all other errors stay errors.
    """
    if not isinstance(calls,list):raise ValueError('tool calls must be an array')
    if len(calls)!=1:return deepcopy(calls),None
    call=calls[0];function=call.get('function',{}) if isinstance(call,dict) else {}
    raw=function.get('arguments') if isinstance(function,dict) else None
    if not isinstance(raw,str):return deepcopy(calls),None
    try:
        strict_json(raw)
        return deepcopy(calls),None
    except json.JSONDecodeError as original:
        original_error=type(original).__name__+': '+str(original)
    if finish_reason!='tool_calls' or call.get('type')!='function' or function.get('name')!='proposal_prepare':
        raise ValueError('complete single proposal_prepare tool response required for suffix normalization')
    # strict_json(prefix) retains duplicate-key and finite-number checks; the
    # raw decoder only locates the boundary, never authorizes arbitrary tails.
    start=len(raw)-len(raw.lstrip(' \t\r\n'))
    _,end=json.JSONDecoder().raw_decode(raw,start)
    tail=raw[end:]
    if tail.strip(' \t\r\n')!='}':raise ValueError('only one surplus final generation brace is eligible')
    prefix=raw[:end];value=strict_json(prefix)
    if not isinstance(value,dict) or set(value) not in ({'spec'},{'spec','baseline_sysml_source'}) or not isinstance(value.get('spec'),dict):
        raise ValueError('complete generation object with object spec required for suffix normalization')
    if 'baseline_sysml_source' in value and not isinstance(value['baseline_sysml_source'],str):raise ValueError('original baseline SysML must be a string')
    import hashlib
    digest=lambda text:hashlib.sha256(text.encode('utf8')).hexdigest()
    position=end+len(tail)-len(tail.lstrip(' \t\r\n'))
    # Delete only the identified character; preserve all whitespace as well.
    normalized=raw[:position]+raw[position+1:]
    if strict_json(normalized)!=value:raise ValueError('generation suffix normalization changed JSON values')
    from .case_paths import sha_value
    audit=dict(schema='v12-generation-final-brace-normalization-1',transformation='REMOVE_SINGLE_SURPLUS_FINAL_OBJECT_BRACE',
        original_arguments_utf8_sha256=digest(raw),normalized_arguments_utf8_sha256=digest(normalized),
        derived_value_sha256=sha_value(value),original_error=original_error,
        removed_character='}',character_offset=position,utf8_byte_offset=len(raw[:position].encode('utf8')),
        complete_object_end=end,original_characters=len(raw),normalized_characters=len(normalized),
        source_strings_changed=False,business_values_changed=False,original_protocol_preserved=True)
    result=deepcopy(calls);result[0]['function']['arguments']=normalized
    return result,audit


def _schema_field_names(schema):
    names = set(schema.get('properties', {}))
    for item in schema.values():
        if isinstance(item, dict): names.update(_schema_field_names(item))
        elif isinstance(item, list):
            for child in item:
                if isinstance(child, dict): names.update(_schema_field_names(child))
    return names


def validate_value(value, schema, path='$', *, _field_names=None):
    """Check the supported internal subset, including wire-omitted limits."""
    if _field_names is None: _field_names = _schema_field_names(schema)
    branches = schema.get('oneOf', schema.get('anyOf'))
    if branches is not None:
        matches, branch_errors = 0, []
        for branch in branches:
            try:
                validate_value(value, branch, path, _field_names=_field_names)
                matches += 1
            except ValueError as exc:
                branch_errors.append(str(exc))
        if not matches or 'oneOf' in schema and matches != 1:
            detail = '; '.join(dict.fromkeys(branch_errors)) if not matches else 'multiple matching branches'
            raise ValueError('tool value does not match schema branch: ' + path + '; ' + detail)
    kind = schema.get('type')
    valid = dict(object=isinstance(value, dict), array=isinstance(value, list), string=isinstance(value, str),
                 number=type(value) in (int, float) and math.isfinite(value), integer=type(value) is int,
                 boolean=type(value) is bool)
    if kind is not None and not valid.get(kind, False):
        raise ValueError('tool value type differs: ' + path)
    if 'enum' in schema and not any((type(value) is type(item) or type(value) in (int, float) and type(item) in (int, float)) and value == item for item in schema['enum']):
        raise ValueError('tool value enum differs: ' + path)
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        raise ValueError('tool value const differs: ' + path)
    if kind == 'string' and 'pattern' in schema:
        import re
        if re.search(schema['pattern'], value) is None:
            raise ValueError('tool string pattern differs: ' + path)
    if kind == 'object':
        props = schema.get('properties', {})
        errors = []
        missing = sorted(set(schema.get('required', [])) - set(value))
        if missing:
            errors.append('tool object fields missing: ' + ', '.join(path + '.' + key for key in missing))
        extras = set(value) - set(props)
        if schema.get('additionalProperties', True) is False and extras:
            # Arbitrary provider keys can contain values or credential text.
            # Only names present somewhere in this schema are echoed.
            labels = sorted({key if key in _field_names else '<unrecognized-field>' for key in extras})
            errors.append('tool object fields unexpected: ' + ', '.join(path + '.' + key for key in labels))
        for key, item in value.items():
            target = props.get(key, schema.get('additionalProperties'))
            if isinstance(target, dict):
                try:
                    label = key if key in _field_names else '<unrecognized-field>'
                    validate_value(item, target, path + '.' + label, _field_names=_field_names)
                except ValueError as exc:
                    errors.append(str(exc))
        if errors: raise ValueError('; '.join(errors))
    if kind == 'array':
        for index, item in enumerate(value):
            validate_value(item, schema['items'], path + '[' + str(index) + ']', _field_names=_field_names)
        if schema.get('uniqueItems') and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise ValueError('tool array items duplicated: ' + path)
    for name, permitted in (('minLength', kind == 'string'), ('maxLength', kind == 'string'), ('minItems', kind == 'array'),
                            ('maxItems', kind == 'array'), ('minProperties', kind == 'object'), ('maxProperties', kind == 'object')):
        if name in schema and permitted and (len(value) < schema[name] if name.startswith('min') else len(value) > schema[name]):
            raise ValueError('tool local size constraint differs: ' + path + ':' + name)
    if kind in ('integer', 'number'):
        for name in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'):
            if name in schema:
                limit = schema[name]
                bad = value < limit if name == 'minimum' else value > limit if name == 'maximum' else value <= limit if name == 'exclusiveMinimum' else value >= limit
                if bad:
                    raise ValueError('tool numeric bound differs: ' + path + ':' + name)
        if 'multipleOf' in schema:
            from decimal import Decimal
            if Decimal(str(value)) % Decimal(str(schema['multipleOf'])) != 0:
                raise ValueError('tool numeric multiple differs: ' + path)


def _decode_wire(value,schema,path='$',audit=None):
    if _empty_schema(schema):
        if value!=EMPTY_MARKER:raise ValueError('exact empty-object marker required: '+path)
        if audit is not None:audit.append(dict(path=path,transformation='EXACT_CLOSED_TRANSPORT_MARKER_TO_DECLARED_EMPTY_OBJECT',before=deepcopy(EMPTY_MARKER),after={}))
        return {}
    branches=schema.get('oneOf',schema.get('anyOf'))
    if branches is not None:
        matches=[]
        for branch in branches:
            try:validate_value(value,wire_schema(branch));matches.append(branch)
            except ValueError:pass
        if len(matches)!=1:raise ValueError('wire branch must match uniquely: '+path)
        return _decode_wire(value,matches[0],path,audit)
    if schema.get('type')=='object':return {key:_decode_wire(item,schema['properties'][key],path+'.'+key,audit) for key,item in value.items()}
    if schema.get('type')=='array':return [_decode_wire(item,schema['items'],path+'['+str(index)+']',audit) for index,item in enumerate(value)]
    return deepcopy(value)


def decode_calls(calls, tools, *, profile=STRICT_PROFILE, finish_reason=None):
    if profile not in STRICT_PROFILES: raise ValueError('strict decoding profile required')
    names = {tool['function']['name']: tool['function']['parameters'] for tool in tools}
    normalized,suffix=generation_suffix_calls(calls,finish_reason);audits=[]
    for call in normalized:
        function = call['function']
        if function['name'] not in names:
            raise ValueError('provider returned unoffered function; no dispatch')
        schema=names[function['name']];raw=function['arguments'];value=strict_json(raw)
        flat = profile == FLAT_PLAN_PROFILE and function['name'] == 'case_request_plan'
        validate_value(value,wire_schema(flat_plan_schema(schema) if flat else schema))
        changes=[suffix] if suffix else []
        if flat:
            value=_unflatten_plan(value)
            changes.append(dict(transformation='VALIDATED_FLAT_PLAN_TO_ORIGINAL_PACKET',profile=profile))
        decoded=_decode_wire(value,schema,audit=changes)
        validate_value(decoded,schema)
        if changes:
            function['arguments']=json.dumps(decoded,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            from .case_paths import sha_value
            audits.append(dict(function=function['name'],raw_arguments_sha256=sha_value(raw),decoded_arguments_sha256=sha_value(function['arguments']),
                               changes=changes,source_strings_changed=False,numeric_values_changed=False,business_semantics_changed=False))
    return normalized,audits


def validate_calls(calls, tools, *, profile=STRICT_PROFILE):
    decode_calls(calls,tools,profile=profile)
