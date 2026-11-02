"""Finite dual-source contract over the pinned SysML parser, not equivalence proof."""
import json, math, re
from .model_artifact import LITERAL_PARAMETER_TYPE, sha

NUMBER=r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
CONTRACT_PROMPT=''' Generate both nonempty artifact.modelica_source and artifact.sysml_source from the SAME approved design. Use exactly this legal outer structure (replace example values and add ALL actual parameters/requirements): `package Foo { part def System { attribute m : Real = 85.0; } part system : System; requirement req_1 { attribute text : String = "EXACT requirement.text"; attribute source_id : String = "EXACT source_id"; attribute source_text : String = "EXACT source_text"; attribute model_name : String = "EXACT artifact.model_name"; } }`. The package MUST open with { and close with }; never use Modelica-style `end package;` or `end Foo;`. Contain exactly one `part def System` and package-level `part system : System;`. Inside System mirror EVERY Modelica numeric parameter as `attribute NAME : Real = EXPRESSION;` (Integer also supported). Preserve the exact arithmetic parameter dependency expression, not its evaluated numeric replacement: e.g. L_nose_CoM = a + L_nose_ext. Only finite numeric literals, parameter names, parentheses and + - * / are supported. Mirror literal parameter numeric values exactly. For every spec.requirements entry in list order add a package-level `requirement req_1 { attribute text : String = "EXACT requirement.text"; attribute source_id : String = "EXACT source_id"; attribute source_text : String = "EXACT source_text"; attribute model_name : String = "EXACT artifact.model_name"; }` then req_2, etc. Escape string values once as JSON strings within source. SysML syntax, shared arithmetic dependency/value consistency and exact requirement traces are checked; full equation/structure equivalence and hardware certification remain UNKNOWN. Do not invent new physics or use duplicate parameters.'''

def normalize_package_wrapper(source):
    """Translate only one unambiguous Modelica-style outer package shell.

    The body bytes survive exactly; this is not a parser or semantic repair.
    Every caller must still run the ordinary SysML and dual-source checks.
    """
    if not isinstance(source,str):return source,None
    match=re.fullmatch(r'(?P<header>[ \t]*package[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]*)(?P<newline>\r?\n)(?P<body>[\s\S]*?)(?P<indent>[ \t]*)(?P<end>end[ \t]+(?P=name)[ \t]*;)(?P<suffix>[ \t]*(?:\r?\n)?)',source)
    if not match:return source,None
    # Keywords in comments/quoted requirement strings are data. Other package
    # declarations or named `end` statements make the shell ambiguous.
    code=re.sub(r'"(?:\\.|[^"\\])*"|/\*[\s\S]*?\*/|//[^\r\n]*',lambda m:' '*len(m[0]),match['body'])
    if re.search(r'\bpackage\b|\bend\s+[A-Za-z_]\w*\s*;',code):raise ValueError('ambiguous nested SysML package/end wrapper; not normalized')
    normalized=match['header']+' {'+match['newline']+match['body']+match['indent']+'}'+match['suffix']
    audit=dict(kind='sysml_outer_package_wrapper',original_sysml_sha256=sha(source.encode()),normalized_sysml_sha256=sha(normalized.encode()),model_original_source=False,body_bytes_preserved=True,transformations=[dict(start=match.end('header'),end=match.end('header'),original='',replacement=' {'),dict(start=match.start('end'),end=match.end('end'),original=match['end'],replacement='}')])
    return normalized,audit

def modelica_literals(source):
    # Remove comments and strings so documentation cannot impersonate declarations.
    clean=re.sub(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*',lambda m:' '*len(m[0]),source,flags=re.S)
    result={}
    for statement in re.findall(r'\bparameter\b[^;]*;',clean):
        match=re.fullmatch(r'parameter\s+('+LITERAL_PARAMETER_TYPE+r')\s+([A-Za-z_]\w*)\s*=\s*('+NUMBER+r')\s*;',statement)
        if not match:raise ValueError('unsupported shared parameter mapping: '+statement.strip()[:180])
        kind,name,value=match.groups();number=float(value)
        if not math.isfinite(number) or (kind=='Integer' and not number.is_integer()):raise ValueError('invalid shared literal: '+name)
        if name in result:raise ValueError('duplicate Modelica parameter name: '+name)
        result[name]=number
    return result

def _nodes(node,kind):
    stack=[node]
    while stack:
        current=stack.pop()
        if type(current).__name__==kind:yield current
        stack.extend(current.getChild(i) for i in range(current.getChildCount()))

def _legacy_inspect_dual_source(spec):
    from week13.src.case_toolchain import check_sysml
    source=spec['artifact'].get('sysml_source','')
    syntax=check_sysml(source)
    report=dict(schema='v11-dual-source-check-1',status='FAIL',scope='SHARED_NUMERIC_LITERALS_AND_EXACT_REQUIREMENT_MODEL_TRACE',equation_and_structure_semantic_equivalence='UNKNOWN',issues=[],modelica_source_sha256=sha(spec['artifact']['modelica_source'].encode()),sysml_source_sha256=sha(source.encode()))
    if syntax.get('status')!='PASS':
        report['issues'].append('SysML parser/minimum structure did not PASS')
        return syntax,report
    try:
        values=modelica_literals(spec['artifact']['modelica_source'])
        systems=syntax['structure']['part_definitions'].get('System',[])
        if len(systems)!=1 or syntax['structure']['part_usages'].count('system')!=1:raise ValueError('required unique System definition/system usage missing')
        attributes=systems[0]['attribute_values']
        if set(attributes)!=set(values):raise ValueError('System shared parameter names differ from Modelica literals')
        for name,value in values.items():
            if len(attributes[name])!=1 or not math.isclose(attributes[name][0],value,abs_tol=1e-12,rel_tol=1e-12):raise ValueError('SysML/Modelica literal mismatch or duplicate: '+name)
            if len(syntax['structure']['attribute_values'].get(name,[]))!=1:raise ValueError('ambiguous duplicate SysML shared parameter: '+name)
        from sysmlpy.antlr_parser import parse
        tree=parse(source)
        usages=[n.getText() for n in _nodes(tree,'PartUsageContext') if re.match(r'partsystem(?:\W|$)',n.getText())]
        if usages!=['partsystem:System;']:raise ValueError('system usage must be explicitly typed by System')
        for node in _nodes(tree,'PartDefinitionContext'):
            if not node.getText().startswith('partdefSystem{'):continue
            for attribute in _nodes(node,'AttributeUsageContext'):
                name=re.match(r'attribute([A-Za-z_]\w*):',attribute.getText())
                if name and name[1] in values and not re.fullmatch(r'attribute'+re.escape(name[1])+r':(?:Real|Integer)='+NUMBER+r';',attribute.getText()):raise ValueError('unsupported SysML shared literal type/expression: '+name[1])
        # Only inspect parser-produced requirement/attribute nodes. Comments or
        # strings elsewhere never count as a requirement or declaration.
        requirements={}
        for node in _nodes(tree,'RequirementUsageContext'):
            name=re.match(r'requirement(req_\d+)\{',node.getText())
            if not name:raise ValueError('unsupported requirement mapping; use req_1, req_2 in list order')
            key=name[1]
            if key in requirements:raise ValueError('duplicate SysML requirement: '+key)
            data={}
            for attribute in _nodes(node,'AttributeUsageContext'):
                match=re.fullmatch(r'attribute(text|source_id|source_text|model_name):String=("(?:\\.|[^"\\])*");',attribute.getText())
                if not match or match[1] in data:raise ValueError('unsupported/duplicate SysML requirement attribute: '+key)
                data[match[1]]=json.loads(match[2])
            requirements[key]=data
        expected={'req_'+str(i):dict(item,model_name=spec['artifact']['model_name']) for i,item in enumerate(spec['requirements'],1)}
        if requirements!=expected:raise ValueError('SysML requirement/source/model trace differs from confirmed task')
        report.update(status='PASS',shared_parameters=values,requirement_traces=requirements)
    except (ValueError,KeyError,TypeError) as exc:report['issues'].append(str(exc))
    return syntax,report

def inspect_dual_source(spec):
    from week13.src.case_toolchain import check_sysml
    from ..case_scope import modelica_parameters,expression_value
    import ast
    source=spec['artifact'].get('sysml_source','');syntax=check_sysml(source)
    report=dict(schema='v12-dual-source-expression-check-1',status='FAIL',scope='FINITE_SHARED_ARITHMETIC_DEPENDENCIES_AND_EXACT_REQUIREMENT_TRACE',equation_and_structure_semantic_equivalence='UNKNOWN',issues=[],modelica_source_sha256=sha(spec['artifact']['modelica_source'].encode()),sysml_source_sha256=sha(source.encode()))
    if syntax.get('status')!='PASS':report['issues'].append('SysML parser/minimum structure did not PASS');return syntax,report
    try:
        from ..case_asym2_family import is_asym2_source,sysml_parameters
        values,expressions,kinds=(sysml_parameters if is_asym2_source(spec['artifact']['modelica_source']) else modelica_parameters)(spec['artifact']['modelica_source'])
        from sysmlpy.antlr_parser import parse
        tree=parse(source);systems=[n for n in _nodes(tree,'PartDefinitionContext') if n.getText().startswith('partdefSystem{')]
        if len(systems)!=1:raise ValueError('required unique part def System missing')
        usages=[n.getText() for n in _nodes(tree,'PartUsageContext') if re.match(r'partsystem(?:\W|$)',n.getText())]
        if usages!=['partsystem:System;']:raise ValueError('system usage must be uniquely typed System')
        mirrors={}
        for node in _nodes(systems[0],'AttributeUsageContext'):
            match=re.fullmatch(r'attribute([A-Za-z_]\w*):(Real|Integer)=(.*);',node.getText())
            if not match:raise ValueError('unsupported System shared attribute')
            name,kind,expression=match.groups()
            if name in mirrors:raise ValueError('duplicate SysML shared parameter')
            mirrors[name]=expression
        if set(mirrors)!=set(values):raise ValueError('SysML/Modelica shared parameter names differ')
        for name,expression in mirrors.items():
            if not math.isclose(expression_value(expression,values),values[name],rel_tol=1e-12,abs_tol=1e-12):raise ValueError('shared parameter value mismatch: '+name)
            # Literal formatting may differ; dependencies may not be flattened or redirected.
            left=ast.parse(expression,mode='eval').body;right=ast.parse(expressions[name],mode='eval').body
            if any(isinstance(n,ast.Name) for n in ast.walk(right)) and ast.dump(left)!=ast.dump(right):raise ValueError('shared parameter dependency expression changed: '+name)
        requirements={}
        for node in _nodes(tree,'RequirementUsageContext'):
            name=re.match(r'requirement(req_\d+)\{',node.getText())
            if not name or name[1] in requirements:raise ValueError('unsupported/duplicate requirement mapping')
            data={}
            for attribute in _nodes(node,'AttributeUsageContext'):
                match=re.fullmatch(r'attribute(text|source_id|source_text|model_name):String=("(?:\\.|[^"\\])*");',attribute.getText())
                if not match or match[1] in data:raise ValueError('unsupported requirement trace attribute')
                data[match[1]]=json.loads(match[2])
            requirements[name[1]]=data
        expected={'req_'+str(i):dict(item,model_name=spec['artifact']['model_name']) for i,item in enumerate(spec['requirements'],1)}
        if requirements!=expected:raise ValueError('SysML requirement/source/model trace differs')
        report.update(status='PASS',shared_parameters=values,shared_expressions=expressions,sysml_expressions=mirrors,requirement_traces=requirements)
    except (ValueError,KeyError,TypeError,SyntaxError) as exc:report['issues'].append(str(exc))
    return syntax,report
