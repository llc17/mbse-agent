"""Source-bound allocation fixture; preparation is inert, runs share CaseBudget.

This does not synthesize or repair a vehicle. The three allocation equations and
wheel radius/torque assignments are copied verbatim from a registered product
source. Prescribed inputs exercise that fragment in DASSL; they are not terrain
or vehicle evidence and never enter the candidate model.
"""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results/contact-numerics-20261022-01'
MODEL='ContactAllocationBoundary'
STOP=1.8
INTERVALS=1800
CASES=[
    'Fn_sum=0 at initial separation and after continuous unloading',
    'Fn_sum=5e-10 and 1e-9 cutoff, then 2e-9 positive domain',
    'continuous noEvent load crossing zero on unloading and recontact',
    'three simultaneous contacts with fractions 0.2/0.3/0.5; other 38 zero',
    'nose friction-limited and front-main torque-limited intervals',
    'positive and negative continuous power_factor with magnitude <=1',
    'all numeric CSV values finite; sum torque and each-contact friction bound',
]


def _sha(raw):return hashlib.sha256(raw).hexdigest()


def extract_fragment(source):
    """Fail closed unless each exact source equation is present once."""
    patterns={
        'Fn_sum':r'\bFn_sum\s*\[\s*i\s*\]\s*=[^;]+;',
        'Ft_budget':r'\bFt_budget\s*\[\s*i\s*\]\s*=[^;]+;',
        'FtC':r'\bFtC\s*\[\s*i\s*,\s*b\s*\]\s*=[^;]+;',
    }
    for name in ('R_w','T_w'):
        for index in (1,2,3):patterns[name+str(index)]=rf'\b{name}\s*\[\s*{index}\s*\]\s*=[^;]+;'
    found={}
    for key,pattern in patterns.items():
        matches=re.findall(pattern,source)
        if len(matches)!=1:raise ValueError('expected one source equation: '+key)
        found[key]=matches[0]
    declarations=re.findall(r'^\s*(parameter\s+(?:Real|Integer|Modelica\.Units\.SI\.\w+)\s+\w+\s*=[^;]+;)',source,re.M)
    if len(declarations)!=25:raise ValueError('expected the exact 25 frozen parameter declarations')
    return found,declarations


def build_fixture(source):
    equations,declarations=extract_fragment(source)
    copied='\n'.join(equations.values())
    model='model '+MODEL+'\n'+'\n'.join('  '+line for line in declarations)+'''
  Real R_w[3], T_w[3], Fn_sum[3], Ft_budget[3];
  Real FnC[3, 2*n_steps+1], FtC[3, 2*n_steps+1];
  Real power_factor;
  Real load_wave;
  Real tiny_load;
  Real probe(start=0, fixed=true);
equation
  load_wave = noEvent(max(0.0, sin(2*Modelica.Constants.pi*(time-0.25))));
  tiny_load = if time < 0.1 then 0.0 else if time < 0.2 then 5e-10
    else if time < 0.3 then 1e-9 else if time < 0.4 then 2e-9 else 800*load_wave;
  power_factor = sin(2*Modelica.Constants.pi*time);
'''
    model+='\n'.join('  '+equations[name+str(i)] for name in ('R_w','T_w') for i in (1,2,3))+'\n'
    model+='''  for i in 1:3 loop
    for b in 1:(2*n_steps+1) loop
      FnC[i,b] = (if b==1 then 0.2 else if b==2 then 0.3 else if b==3 then 0.5 else 0.0)
        * (if i==1 then 100*load_wave else if i==2 then 2000*load_wave else tiny_load);
    end for;
'''
    model+='    '+equations['Fn_sum']+'\n    '+equations['Ft_budget']+'\n'
    model+='    for b in 1:(2*n_steps+1) loop\n      '+equations['FtC']+'\n    end for;\n  end for;\n'
    model+='''  der(probe) = Fn_sum[1]+Fn_sum[2]+Fn_sum[3];
  annotation(experiment(StartTime=0, StopTime=1.8, Interval=0.001, Tolerance=0.01));
end ContactAllocationBoundary;
'''
    return model,dict(equations=equations,equation_sha256={k:_sha(v.encode('utf8')) for k,v in equations.items()},
                      combined_equations_sha256=_sha(copied.encode('utf8')),
                      parameter_declarations_sha256=_sha('\n'.join(declarations).encode('utf8')),
                      candidate_source_sha256=_sha(source.encode('utf8')))


def _trusted(source):
    from .case_contact_family import support_for_reviewed_source
    from .case_contact_numerics import PACKAGE_SHA
    support=support_for_reviewed_source(source)
    if not support or support.get('approved_package_sha256')!=PACKAGE_SHA or not support.get('contact_domain',{}).get('geometry_unchanged'):
        raise ValueError('boundary requires the exact newly reviewed numerical-only product family')
    return support


def prepare(source_path,out):
    source_path=Path(source_path).resolve();out=Path(out).resolve()
    if not source_path.is_relative_to(ROOT) or source_path.is_relative_to(ROOT/'V12_old') or not out.is_relative_to(OUT) or out==OUT:
        raise ValueError('boundary paths must stay in current approved storage')
    source=source_path.read_text(encoding='utf8');support=_trusted(source)
    model,extraction=build_fixture(source)
    out.mkdir(parents=True,exist_ok=False)
    path=out/'fixture.mo';path.write_text(model,encoding='utf8',newline='\n')
    receipt=dict(schema='v12-source-bound-contact-boundary-preparation-1',status='PREPARED_NOT_RUN',
                 product_source_path=str(source_path),product_source_sha256=_sha(source.encode('utf8')),
                 product_source_support=support,fixture_path=str(path),fixture_sha256=_sha(path.read_bytes()),
                 extraction=extraction,cases=CASES,stop_time_s=STOP,intervals=INTERVALS,solver='dassl',
                 physical_status='UNKNOWN',scope='SOURCE_FRAGMENT_NUMERICAL_BOUNDARY_NOT_VEHICLE_RUN',
                 planned_compile=1,planned_simulation=1,new_HTTP=0,new_compile=0,new_simulation=0)
    (out/'prepared.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    return receipt


def compiled_constants(source,xml_path,initializer_path):
    """Read direct XML parameters and verify actual compiled alias assignments.

    Calculated R_w/T_w have no XML start and are not runtime observations.
    Their values follow the emitted initializer's checked parameter mapping.
    """
    from .case_scope import modelica_parameters
    expected=modelica_parameters(source)[0];equations,_=extract_fragment(source)
    xml_path=Path(xml_path);initializer_path=Path(initializer_path)
    tree=ET.parse(xml_path);root=tree.getroot()
    if root.get('modelName')!=MODEL:raise ValueError('compiled boundary XML model identity differs')
    names=('R_main','R_nose','T_main_max','T_nose_max','mu_s')
    aliases={name+f'[{i}]':('R_nose' if i==1 else 'R_main') if name=='R_w' else ('T_nose_max' if i==1 else 'T_main_max') for name in ('R_w','T_w') for i in (1,2,3)}
    nodes={};direct={};indices={}
    for node in root.iter('ScalarVariable'):
        name=node.get('name')
        if name not in {*names,*aliases}:continue
        if name in nodes:raise ValueError('duplicate compiled boundary parameter: '+name)
        if node.get('variability')!='parameter' or node.get('classType')!='rPar' or node.get('alias')!='noAlias':raise ValueError('unexpected compiled boundary parameter kind: '+name)
        if Path(node.get('fileName','')).resolve()!=(xml_path.parent/'model.mo').resolve():raise ValueError('compiled parameter source file differs')
        nodes[name]=node;indices[name]=int(node.attrib['classIndex'])
        real=node.find('Real')
        if real is None or real.get('fixed')!='true':raise ValueError('compiled parameter is not fixed Real: '+name)
        if name in names:
            if node.get('causality')!='parameter' or real.get('start') is None:raise ValueError('direct XML parameter value absent: '+name)
            value=float(real.attrib['start'])
            if not math.isfinite(value) or value!=expected[name]:raise ValueError('compiled XML/source parameter conflict: '+name)
            direct[name]=value
        elif node.get('causality')!='calculatedParameter' or real.get('start') is not None:
            raise ValueError('expected calculated boundary parameter without XML start: '+name)
    if set(nodes)!={*names,*aliases}:raise ValueError('compiled boundary parameter nodes incomplete')
    emitted=initializer_path.read_text(encoding='utf8')
    pattern=r'\(data->simulationInfo->realParameter\[(\d+)\]\s*/\*\s*(R_w\[\d+\]|T_w\[\d+\])\s+PARAM\s*\*/\)\s*=\s*\(data->simulationInfo->realParameter\[(\d+)\]\s*/\*\s*([A-Za-z_]\w*)\s+PARAM\s*\*/\)\s*;'
    found={};bindings={}
    for lhs_index,lhs,rhs_index,rhs in re.findall(pattern,emitted):
        if lhs in found:raise ValueError('duplicate compiled alias initializer: '+lhs)
        found[lhs]=(int(lhs_index),rhs,int(rhs_index))
    values={'mu_s':direct['mu_s']}
    for alias,target in aliases.items():
        if found.get(alias)!=(indices[alias],target,indices[target]):raise ValueError('compiled initializer/source alias mapping differs: '+alias)
        key=alias.replace('[','').replace(']','')
        if re.sub(r'\s+','',equations[key])!=alias+'='+target+';':raise ValueError('extracted product alias equation differs: '+alias)
        values[alias]=direct[target]
        bindings[alias]=dict(target=target,compiled_lhs_index=indices[alias],compiled_rhs_index=indices[target],
                            xml_causality='calculatedParameter',xml_start_present=False,
                            value_basis='DIRECT_XML_PARAMETER_PLUS_VERIFIED_COMPILED_INITIALIZER_ASSIGNMENT')
    return dict(schema='v12-boundary-compiled-constant-readback-1',status='PASS',values=values,direct_XML_values=direct,
                alias_bindings=bindings,xml_sha256=_sha(xml_path.read_bytes()),initializer_sha256=_sha(initializer_path.read_bytes()),
                observation_role='COMPILED_INITIAL_VALUES_AND_COMPILED_ALIAS_EVALUATION_NOT_RUNTIME_OUTPUT')


def check_csv(path,*,constants=None):
    """Compare the real fragment with its original nonzero-domain invariants."""
    counts=dict(zero=0,near_zero_cutoff=0,exact_cutoff=0,positive_domain=0,multiple_contact=0,
                positive_power=0,negative_power=0,friction_limited=0,torque_limited=0,
                unload_crossings=0,recontacts=0)
    issues=[];previous={};minimum_time=None;last_time=None;rows=0;maximum_torque_excess=0.;maximum_friction_excess=0.
    with Path(path).open(encoding='utf-8-sig',newline='') as handle:
        reader=csv.DictReader(handle)
        if not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames)):raise ValueError('missing/duplicate boundary columns')
        for raw in reader:
            row={k:float(v) for k,v in raw.items()}
            if any(not math.isfinite(v) for v in row.values()):raise ValueError('nonfinite boundary CSV')
            t=row['time'];rows+=1
            if last_time is not None and t<last_time:raise ValueError('boundary time reversed')
            if minimum_time is None:minimum_time=t
            last_time=t;pf=row['power_factor'];counts['positive_power']+=int(pf>.1);counts['negative_power']+=int(pf<-.1)
            def constant(name):
                if name in row:
                    if constants is not None and row[name]!=constants[name]:raise ValueError('CSV/compiled constant conflict: '+name)
                    return row[name]
                if constants is None or name not in constants:raise ValueError('bound compiled constant evidence required: '+name)
                return constants[name]
            for i in (1,2,3):
                normal=row[f'Fn_sum[{i}]'];radius=constant(f'R_w[{i}]');torque=constant(f'T_w[{i}]');mu=constant('mu_s')
                normals=[row[f'FnC[{i},{b}]'] for b in range(1,42)]
                forces=[row[f'FtC[{i},{b}]'] for b in range(1,42)]
                budget=row[f'Ft_budget[{i}]']
                def roundoff(*values):return 128*max(math.ulp(abs(value)) for value in values)
                active=normal>0.;prev=previous.get(i)
                if prev is not None and t>prev[0]:
                    counts['unload_crossings']+=int(prev[1] and not active)
                    counts['recontacts']+=int(not prev[1] and active)
                previous[i]=(t,active)
                counts['zero']+=int(normal==0.);counts['near_zero_cutoff']+=int(0.<normal<=1e-9)
                counts['exact_cutoff']+=int(normal==1e-9)
                counts['positive_domain']+=int(normal>1e-9);counts['multiple_contact']+=int(sum(v>0. for v in normals)>=3)
                counts['friction_limited']+=int(normal>1e-8 and mu*normal<torque/radius)
                counts['torque_limited']+=int(normal>1e-8 and mu*normal>torque/radius)
                torque_excess=sum(map(abs,forces))*radius-torque
                friction_excess=max(abs(ft)-mu*fn for ft,fn in zip(forces,normals))
                maximum_torque_excess=max(maximum_torque_excess,torque_excess);maximum_friction_excess=max(maximum_friction_excess,friction_excess)
                violation=normal<0. or any(fn<0. for fn in normals) or abs(pf)>1.+roundoff(1.,pf) or torque_excess>roundoff(torque,sum(map(abs,forces))*radius)
                violation|=any(abs(ft)-mu*fn>roundoff(ft,mu*fn) for ft,fn in zip(forces,normals))
                violation|=abs(normal-sum(normals))>roundoff(normal,*normals)
                if normal<=1e-9:violation|=any(ft!=0. for ft in forces)
                else:
                    # The original reviewed allocation is evaluated only where
                    # its denominator is nonzero. This is not a repaired formula.
                    violation|=any(abs(ft-budget*fn/normal)>roundoff(ft,budget*fn/normal) for ft,fn in zip(forces,normals))
                if violation and len(issues)<20:issues.append(dict(time=t,wheel=i,normal=normal,power_factor=pf,torque_excess=torque_excess,friction_excess=friction_excess))
    if minimum_time is None or abs(minimum_time)>1e-12 or abs(last_time-STOP)>STOP*1e-9:issues.append(dict(reason='boundary observation incomplete'))
    missing=[key for key,value in counts.items() if not value]
    return dict(schema='v12-source-bound-contact-boundary-check-1',status='FAIL' if issues or missing else 'PASS',
                rows=rows,time_start=minimum_time,time_end=last_time,counters=counts,issues=issues,missing_cases=missing,
                maximum_torque_excess_Nm=maximum_torque_excess,maximum_contact_friction_excess_N=maximum_friction_excess,
                arithmetic_tolerance='128 ULP per compared operation/value; cutoff Ft must equal 0 exactly; no absolute physical acceptance threshold',
                physical_status='UNKNOWN',scope='EXTRACTED_ALLOCATION_ONLY_NOT_FULL_CONTACT_OR_VEHICLE_CERTIFICATION')


def recheck(prepared,receipt):
    """Read the already terminal tool evidence; launch nothing, replace nothing."""
    from datetime import datetime,timezone
    prepared=Path(prepared).resolve();receipt=Path(receipt).resolve()
    if not prepared.is_relative_to(OUT) or prepared.name!='prepared.json' or receipt.parent!=prepared.parent or receipt.name in ('prepared.json','boundary-result.json') or receipt.exists():raise ValueError('readback requires a new sibling receipt inside the approved boundary')
    folder=prepared.parent;record=json.loads(prepared.read_text(encoding='utf8'));old_path=folder/'boundary-result.json';old=json.loads(old_path.read_text(encoding='utf8'))
    product_path=Path(record['product_source_path']).resolve()
    if not product_path.is_relative_to(ROOT) or product_path.is_relative_to(ROOT/'V12_old'):raise ValueError('bound product source outside current storage')
    paths=[prepared,old_path,folder/'fixture.mo',product_path,*[folder/'tool'/name for name in ('model.mo','model_res.csv','model_init.xml','model_08bnd.c','tool-result.json','runtime-parameter-output.json','simulation.stdout.log','simulation.stderr.log','build.mos','build.stdout.log','build.stderr.log','model.exe')]]
    protected={str(path):_sha(path.read_bytes()) for path in paths}
    source=product_path.read_text(encoding='utf8');model,extraction=build_fixture(source)
    tool=json.loads((folder/'tool/tool-result.json').read_text(encoding='utf8'))
    process=json.loads((folder/'tool/runtime-parameter-output.json').read_text(encoding='utf8'))
    if record['extraction']!=extraction or record['product_source_sha256']!=_sha(source.encode('utf8')) or record['fixture_sha256']!=_sha(model.encode('utf8')) or (folder/'fixture.mo').read_bytes()!=model.encode('utf8') or (folder/'tool/model.mo').read_bytes()!=model.encode('utf8'):raise ValueError('source/prepared/actual compiled fixture drift')
    if old['prepared_sha256']!=_sha(prepared.read_bytes()) or old['fixture_sha256']!=record['fixture_sha256'] or old['tool_outcome']!=tool or tool['source_sha256']!=record['fixture_sha256'] or old['product_source_sha256']!=record['product_source_sha256']:raise ValueError('original boundary outcome/source binding drift')
    if tool['compile']['status']!='PASS' or tool['simulation']['status']!='PASS' or tool['simulation']['returncode']!=0 or tool['simulation']['timed_out'] is not False or tool['simulation']['uncertain'] is not False:raise ValueError('readback requires known successful original compile/simulation')
    if process.get('source_sha256')!=record['fixture_sha256'] or process.get('stdout_sha256')!=_sha((folder/'tool/simulation.stdout.log').read_bytes()) or process.get('task_id')!=old['task_id'] or process.get('returncode')!=0 or process.get('timed_out') is not False or process.get('uncertain') is not False:raise ValueError('same-run source/stdout process receipt differs')
    command=process.get('command',[])
    if '-s=dassl' not in command or any(str(arg).startswith(('-override','-f=')) for arg in command):raise ValueError('unexpected solver or parameter override in original process')
    context_path=ROOT/'runs/case-projects/case_3b0265f496f84ae9b800/requests'/old['request_id']/'task-context.json'
    ctx=json.loads(context_path.read_text(encoding='utf8'))
    with closing(sqlite3.connect(Path(ctx['runtime_authority']['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        attempts=[dict(row) for row in db.execute("SELECT * FROM process_attempts WHERE kind IN ('compile','simulation')")]
    actual_attempts=[]
    for attempt in attempts:
        detail=json.loads(attempt['detail_json'])
        if detail.get('task_id')!=old['task_id'] or Path(detail.get('cwd','')).resolve()!=(folder/'tool').resolve():continue
        outcome=json.loads(attempt['result_json'])
        if attempt['state']!='FINISHED' or not attempt['launched_at'] or outcome.get('returncode')!=0 or outcome.get('timed_out') is not False or outcome.get('uncertain') is not False:raise ValueError('original owned boundary process is not terminal PASS')
        if attempt['kind']=='simulation' and (attempt['attempt_id']!=process['attempt_id'] or detail['command']!=command or attempt['pid']!=process['pid']):raise ValueError('original simulation launch/receipt identity differs')
        actual_attempts.append(dict(attempt_id=attempt['attempt_id'],kind=attempt['kind'],state=attempt['state'],pid=attempt['pid']))
    if sorted(row['kind'] for row in actual_attempts)!=['compile','simulation']:raise ValueError('exactly one owned original compile and simulation required')
    constants=compiled_constants(model,folder/'tool/model_init.xml',folder/'tool/model_08bnd.c')
    check=check_csv(folder/'tool/model_res.csv',constants=constants['values'])
    after={str(path):_sha(path.read_bytes()) for path in paths}
    if protected!=after:raise ValueError('protected evidence changed during readback')
    result=dict(schema='v12-source-bound-contact-boundary-zero-process-recheck-1',recorded_at_utc=datetime.now(timezone.utc).isoformat(),
                status=check['status'],request_id=old['request_id'],task_id=old['task_id'],product_source_sha256=record['product_source_sha256'],fixture_sha256=record['fixture_sha256'],
                original_boundary_result_sha256=protected[str(old_path)],original_boundary_check=old['boundary_check'],
                original_failure_preserved=True,compiled_constant_readback=constants,boundary_check=check,
                actual_prior_process_attempts=actual_attempts,protected_files_unchanged=True,protected_file_sha256=after,
                checker_source_sha256=_sha(Path(__file__).read_bytes()),new_HTTP=0,new_compile=0,new_simulation=0,
                physical_status='UNKNOWN',limits='Only the already-run finite allocation fixture; no complete vehicle or all-Newton-state certification.')
    with receipt.open('x',encoding='utf8') as handle:handle.write(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return result


def run(prepared,request_id,task_id):
    """One main-authorized invocation; CaseBudget owns every actual launch."""
    if not re.fullmatch(r'contact_boundary_[A-Za-z0-9_-]+',task_id):raise ValueError('distinct boundary task identity required')
    os.environ['MBSE_V12_REQUEST_ID']=request_id;os.environ['V12_CASE_TASK_ID']=task_id
    from . import case_paths
    from .case_contact_numerics import PACKAGE_SHA
    from .case_contact_family import contact_authority
    if not case_paths.TASK_CONTEXT or case_paths.TASK_CONTEXT['request_id']!=request_id:raise ValueError('boundary requires a fresh process with the selected current request')
    authority,_=contact_authority(case_paths.TASK_CONTEXT)
    if authority['contact_repair_package']['sha256']!=PACKAGE_SHA:raise PermissionError('old package cannot run a new boundary check')
    prepared=Path(prepared).resolve()
    if not prepared.is_relative_to(OUT) or prepared.name!='prepared.json':raise ValueError('invalid boundary preparation path')
    record=json.loads(prepared.read_text(encoding='utf8'))
    source_path=Path(record['product_source_path']).resolve()
    if not source_path.is_relative_to(ROOT) or source_path.is_relative_to(ROOT/'V12_old'):raise ValueError('boundary product source path escaped current storage')
    source=source_path.read_text(encoding='utf8');support=_trusted(source)
    model,extraction=build_fixture(source);fixture=prepared.parent/'fixture.mo'
    if record['schema']!='v12-source-bound-contact-boundary-preparation-1' or record['product_source_support']!=support or record['extraction']!=extraction or record['fixture_sha256']!=_sha(fixture.read_bytes()) or fixture.read_bytes()!=model.encode('utf8'):
        raise ValueError('boundary exact source/fragment/preparation binding drift')
    from .case_toolchain import execute_model
    outcome=execute_model(model,MODEL,prepared.parent/'tool',STOP,INTERVALS,simulation_timeout_s=900)
    check=dict(status='NOT_RUN',reason='tool prerequisite failed',physical_status='UNKNOWN')
    if outcome['simulation']['status']=='PASS':
        try:
            constants=compiled_constants(model,prepared.parent/'tool/model_init.xml',prepared.parent/'tool/model_08bnd.c')
            check=check_csv(prepared.parent/'tool/model_res.csv',constants=constants['values'])
        except (OSError,ValueError,KeyError,TypeError) as exc:check=dict(status='FAIL',issues=[str(exc)],physical_status='UNKNOWN')
    result=dict(schema='v12-source-bound-contact-boundary-run-1',request_id=request_id,task_id=task_id,
                approved_package_sha256=PACKAGE_SHA,prepared_sha256=_sha(prepared.read_bytes()),
                fixture_sha256=record['fixture_sha256'],product_source_sha256=record['product_source_sha256'],
                tool_outcome=outcome,boundary_check=check,new_HTTP=0,
                accounting='compile/simulation launches counted in the selected shared 8/8 CaseBudget',physical_status='UNKNOWN')
    case_paths.save_json(prepared.parent/'boundary-result.json',result,immutable=True)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='同源FtC边界检查；prepare不启动任何进程，run由主窗调用并同账计数')
    commands=parser.add_subparsers(dest='action',required=True)
    prep=commands.add_parser('prepare');prep.add_argument('--source',required=True);prep.add_argument('--out',required=True)
    execute=commands.add_parser('run');execute.add_argument('--prepared',required=True);execute.add_argument('--request-id',required=True);execute.add_argument('--task-id',required=True)
    readback=commands.add_parser('recheck');readback.add_argument('--prepared',required=True);readback.add_argument('--receipt',required=True)
    args=parser.parse_args(argv)
    result=prepare(args.source,args.out) if args.action=='prepare' else recheck(args.prepared,args.receipt) if args.action=='recheck' else run(args.prepared,args.request_id,args.task_id)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
