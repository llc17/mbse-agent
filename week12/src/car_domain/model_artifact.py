"""Restricted Modelica source artifact interface, independent of any vehicle template.

This is a supported-source boundary, not a sandbox for arbitrary Modelica.
The product model supplies source; trusted local context supplies permissions/caps.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import csv, hashlib, json, math, re
from .contracts import inside_week12
from week11.src.store import digest_json
from .artifact_trajectory import reject,child_path

BACKEND='model-artifact'
SCHEMA='v11-model-artifact-1'
NAME=r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*'
SUPPORTED_MSL={'Blocks','Mechanics','Electrical','Thermal','Units','Constants','Math','SIunits'}
# Explicit MSL 4.0.0 Real aliases, not arbitrary qualified or user-defined types.
# Source: https://github.com/modelica/ModelicaStandardLibrary/blob/v4.0.0/Modelica/Units.mo
SI_REAL_ALIASES=frozenset({'Mass','Length','Velocity','Acceleration','Angle','AngularVelocity','AngularAcceleration','Time','Force','Torque','MomentOfForce','MomentOfInertia','Inertia','Energy','Power'})
LITERAL_PARAMETER_TYPE=r'(?:Real|Integer|Modelica\.Units\.SI\.(?:'+ '|'.join(sorted(SI_REAL_ALIASES)) +r'))'

def sha(raw):return hashlib.sha256(raw).hexdigest()
def save(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
def finite(value):return type(value) in (int,float) and math.isfinite(value)

@dataclass(frozen=True)
class ExecutionContext:
    path: Path
    digest: str
    context_id: str
    mode: str
    allow_execution: bool
    max_tasks: int
    worker_timeout_s: int
    simulation_timeout_s: int
    test_tool_failure: bool=False

    @classmethod
    def load(cls,path):
        path=inside_week12(path);raw=path.read_bytes();value=json.loads(raw)
        required={'schema','context_id','status','mode','allow_execution','max_tasks','worker_timeout_s','simulation_timeout_s'}
        if not required<=set(value) or set(value)-required-{'test_tool_failure'}:
            raise ValueError('unsupported trusted execution context')
        if value['schema']!='v11-artifact-execution-context-1' or value['status']!='ACTIVE':
            raise ValueError('closed/unapproved execution context cannot execute')
        if not re.fullmatch(r'[A-Za-z0-9_-]+',value['context_id']) or value['mode'] not in ('LIVE','TEST_ONLY','TEST_ONLY_REAL_OMC'):
            raise ValueError('invalid trusted execution identity')
        for field,limit in [('max_tasks',8),('worker_timeout_s',300),('simulation_timeout_s',120)]:
            if type(value[field]) is not int or not 0<value[field]<=limit:raise ValueError('invalid finite caller cap: '+field)
        if type(value['allow_execution']) is not bool:raise ValueError('invalid caller permission')
        fake=value.get('test_tool_failure',False)
        if type(fake) is not bool or (fake and value['mode']!='TEST_ONLY'):raise ValueError('test injection cannot enter live backend')
        return cls(path,sha(raw),value['context_id'],value['mode'],value['allow_execution'],value['max_tasks'],value['worker_timeout_s'],value['simulation_timeout_s'],fake)

    def assert_current(self):
        if ExecutionContext.load(self.path)!=self:raise ValueError('trusted execution context changed')

def validate_artifact(spec,*,require_sysml=False):
    try:
        fields={'schema_version','backend','artifact','sources','requirements','design','libraries','settings','outputs','checks'}
        if not isinstance(spec,dict) or not fields<=set(spec) or set(spec)-fields-{'baseline_ref','trajectory_contract'} or spec['schema_version']!=SCHEMA or spec['backend']!=BACKEND:
            raise ValueError('unsupported model artifact schema/fields')
        if 'baseline_ref' in spec:
            from .artifact_baseline import REF_FIELDS
            ref=spec['baseline_ref']
            if not isinstance(ref,dict) or set(ref)!=REF_FIELDS or any(not isinstance(v,str) or not v for v in ref.values()):raise ValueError('invalid artifact baseline reference')
            if any(not re.fullmatch(r'[a-f0-9]{64}',ref[k]) for k in ('spec_digest','source_sha256','manifest_sha256')):raise ValueError('invalid artifact baseline digest')
        artifact=spec['artifact']
        if not isinstance(artifact,dict) or set(artifact) not in ({'modelica_source','model_name'},{'modelica_source','model_name','sysml_source'}):
            raise ValueError('artifact must contain only source and qualified class, optional SysML')
        source=artifact['modelica_source'];name=artifact['model_name']
        if not isinstance(source,str) or not source.strip() or len(source.encode('utf8'))>131072 or not isinstance(name,str) or not re.fullmatch(NAME,name):
            raise ValueError('invalid Modelica source or qualified model class')
        # Deliberately conservative lexical support boundary: comments containing
        # forbidden features are refused too. Do not claim arbitrary-code isolation.
        forbidden=r'\b(?:external|import|System|OpenModelica|loadResource|readFile|writeFile|system|popen)\b|Modelica\.Utilities\b|__OpenModelica|\b(?:Library|Include|LibraryDirectory|IncludeDirectory)\s*=' 
        if re.search(forbidden,source,re.I) or re.search(r'"[^"\n]*(?:https?://|file:|modelica://|[A-Za-z]:[\\/])',source):
            raise ValueError('UNSUPPORTED_SOURCE_FEATURE: external/file/process/resource access')
        if not re.search(r'\bmodel\s+'+re.escape(name.split('.')[-1])+r'\b',source):
            raise ValueError('qualified model leaf is absent from source')
        for used in re.findall(r'\bModelica\.([A-Za-z_]\w*)',source):
            if used not in SUPPORTED_MSL:raise ValueError('unsupported standard-library area: '+used)
        if spec['libraries'] not in ([],['Modelica']):raise ValueError('only builtins and installed Modelica standard library supported')
        if 'sysml_source' in artifact and (not isinstance(artifact['sysml_source'],str) or len(artifact['sysml_source'].encode('utf8'))>131072):
            raise ValueError('invalid optional SysML source')
        declared=spec['sources']
        if not isinstance(declared,list) or not declared or any(not isinstance(s,dict) or set(s)!={'id','text'} or not all(isinstance(s[k],str) and s[k].strip() for k in ('id','text')) for s in declared):
            raise ValueError('trusted sources required')
        sources={s['id']:s['text'] for s in declared}
        if len(sources)!=len(declared):raise ValueError('duplicate source id')
        def bound(item,path):
            if not isinstance(item.get('source_id'),str) or item['source_id'] not in sources:
                reject('exact trusted user-source substring required',path+'.source_id',item.get('source_id'),'known trusted source ID',allowed_source_ids=list(sources))
            if not isinstance(item.get('source_text'),str) or not item['source_text'].strip() or item['source_text'] not in sources[item['source_id']]:
                reject('exact trusted user-source substring required',path+'.source_text',item.get('source_text'),'exact nonempty substring of trusted source '+item['source_id'])
        if not isinstance(spec['requirements'],list) or not spec['requirements']:raise ValueError('source-bound requirements required')
        for index,requirement in enumerate(spec['requirements']):
            if not isinstance(requirement,dict) or set(requirement)!={'text','source_id','source_text'} or not isinstance(requirement['text'],str):raise ValueError('invalid requirement')
            bound(requirement,'$.requirements['+str(index)+']')
        design=spec['design']
        if not isinstance(design,dict) or set(design)!={'summary','assumptions','parameters'} or not isinstance(design['summary'],str) or not design['summary'].strip():raise ValueError('model design summary required')
        for assumption in design['assumptions']:
            if not isinstance(assumption,dict) or set(assumption)!={'text','origin'} or assumption['origin']!='model' or not isinstance(assumption['text'],str):raise ValueError('model assumptions must remain separate from user facts')
        params=design['parameters']
        if not isinstance(params,list) or len(params)>64:raise ValueError('invalid model parameter metadata')
        for parameter in params:
            if not isinstance(parameter,dict) or set(parameter)!={'name','value','unit','origin','basis'} or parameter['origin']!='model' or not finite(parameter['value']):raise ValueError('invalid model-origin parameter')
            for field in ('name','unit','basis'):
                if not isinstance(parameter[field],str) or not parameter[field].strip():raise ValueError('invalid model parameter explanation')
            pattern=r'\bparameter\s+'+LITERAL_PARAMETER_TYPE+r'\s+'+re.escape(parameter['name'])+r'\s*=\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*(?:"(?:\\.|[^"\\])*"\s*)?;'
            match=re.search(pattern,source)
            if not match or not math.isclose(float(match[1]),parameter['value'],abs_tol=1e-12,rel_tol=1e-12):raise ValueError('model parameter metadata differs from literal source: '+parameter['name'])
        settings=spec['settings']
        if not isinstance(settings,dict) or set(settings)!={'stop_time','intervals'} or not finite(settings['stop_time']) or not 0<settings['stop_time']<=120 or type(settings['intervals']) is not int or not 2<=settings['intervals']<=4000:
            raise ValueError('unsupported finite simulation settings')
        outputs=spec['outputs']
        if not isinstance(outputs,dict) or not outputs or len(outputs)>64:reject('invalid output mapping','$.outputs',outputs,'object with 1..64 label-to-CSV-variable mappings')
        for label,variable in outputs.items():
            if not isinstance(label,str) or not label.strip() or len(label)>128 or any(ord(c)<32 or ord(c)==127 for c in label):reject('invalid output mapping',child_path('$.outputs',label),label,'nonempty label <=128 characters without control characters')
            if not isinstance(variable,str) or not re.fullmatch(NAME,variable):reject('invalid output mapping',child_path('$.outputs',label),variable,'qualified CSV variable string matching '+NAME)
        if not isinstance(spec['checks'],list) or len(spec['checks'])>16:raise ValueError('invalid observation checks')
        for index,check in enumerate(spec['checks']):
            if not isinstance(check,dict) or set(check)!={'kind','output','threshold','source_id','source_text'} or check['kind'] not in ('final_ge','final_le') or check['output'] not in outputs or not finite(check['threshold']):raise ValueError('unsupported observation check')
            bound(check,'$.checks['+str(index)+']')
            numbers=re.findall(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?',check['source_text'])
            if not any(math.isclose(float(v),check['threshold'],abs_tol=1e-12,rel_tol=1e-12) for v in numbers):raise ValueError('observation threshold absent from user source')
        if 'trajectory_contract' in spec:
            from .artifact_trajectory import validate_contract
            validate_contract(spec['trajectory_contract'],spec)
        if require_sysml:
            if not artifact.get('sysml_source','').strip():raise ValueError('required SysML source missing or empty; historical records remain read-only')
            from .artifact_sysml import inspect_dual_source
            syntax,consistency=inspect_dual_source(spec)
            if syntax.get('status')!='PASS' or consistency['status']!='PASS':raise ValueError('required SysML/dual-source check failed: '+str(syntax.get('issues',[])+consistency['issues']))
        canonical=deepcopy(spec)
        return dict(ready=True,spec=canonical,spec_digest=digest_json(canonical),source_sha256=sha(source.encode('utf8')),issues=[])
    except (ValueError,TypeError,KeyError) as exc:
        return dict(ready=False,issues=[dict(getattr(exc,'issue',{}),message=str(exc))])

def summarize_execution(spec,tool_result,csv_path):
    tool_ok=all(tool_result.get(k,{}).get('status')=='PASS' for k in ('compile','simulation'))
    result=dict(schema_version='v11-artifact-outcome-1',backend=BACKEND,tool_status='PASS' if tool_ok else 'TOOL_FAIL',chain_status='PASS' if tool_ok else 'BLOCKED_TOOL',task_status='UNKNOWN',vehicle_task_status='UNKNOWN',physical_acceptance_status='NOT_VERIFIED',published_version_id=None,observations=[],tool_result=tool_result)
    if not tool_ok:return result
    from .artifact_delivery import read_signals,SignalEvidenceError
    try:samples=read_signals(spec,csv_path)
    except SignalEvidenceError as exc:
        result.update(chain_status='OUTPUT_CONTRACT_MISMATCH' if exc.missing else 'BLOCKED_EVIDENCE',task_status='UNKNOWN',output_mapping=exc.mapping,evidence_issues=[str(exc)])
        if exc.missing:result['missing_output_columns']=exc.missing
        return result
    result['output_mapping']=samples['mapping']
    result['output_contract_status']='PASS'
    if 'trajectory_contract' in spec:
        from .artifact_trajectory import evaluate_trajectory
        result['trajectory_assessment']=evaluate_trajectory(spec,csv_path)
        result['task_status']=result['trajectory_assessment']['task_status']
        result['task_scope']=result['trajectory_assessment']['scope']
    if not spec['checks']:return result
    missing=False
    for check in spec['checks']:
        column=result['output_mapping'][check['output']]['resolved']
        try:value=samples['series'][check['output']][-1]
        except (KeyError,ValueError,TypeError):missing=True;result['observations'].append(dict(output=column,status='UNKNOWN'));continue
        if not math.isfinite(value):raise ValueError('nonfinite actual observation')
        passed=value>=check['threshold'] if check['kind']=='final_ge' else value<=check['threshold']
        result['observations'].append(dict(output=column,value=value,criterion=check,status='PASS' if passed else 'FAIL'))
    check_status='UNKNOWN' if missing else ('FAIL' if any(o['status']=='FAIL' for o in result['observations']) else 'PASS')
    if 'trajectory_assessment' in result:
        statuses=(check_status,result['trajectory_assessment']['task_status'])
        result['task_status']='UNKNOWN' if 'UNKNOWN' in statuses else 'FAIL' if 'FAIL' in statuses else 'PASS'
    else:result['task_status']=check_status
    result['task_scope']='SOURCE_BOUND_SIGNAL_AND_ORDERED_TRAJECTORY_OBSERVATIONS_NOT_VEHICLE_ACCEPTANCE' if 'trajectory_assessment' in result else 'SOURCE_BOUND_SIGNAL_OBSERVATIONS_NOT_INDEPENDENT_VEHICLE_ACCEPTANCE'
    return result
