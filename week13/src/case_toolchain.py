"""Inherited OMC/CSV runner with one deadline, launch accounting and explicit DASSL."""
import hashlib,json,math,os,re,subprocess,time
from contextvars import ContextVar
from pathlib import Path
from .case_paths import ROOT,remaining,require_time,authorization,inside_write,process_attempt,mark_process_launched,finish_process
from .inherited import toolchain_original as _runner
from week9.src.modelica_tool import _terminate_process_tree
from .case_progress import emit_progress

_runner._WEEK12_ROOT=ROOT
check_sysml=_runner.check_sysml
_tolerance=.01
_execution_parameter_names=ContextVar('v12_runtime_parameter_names',default=None)
_execution_output_names=ContextVar('v12_runtime_output_names',default=())

def _parameter_names(parameter_names=None):
    from .case_scope import COMPILED_PARAMETER_NAMES
    if parameter_names is None:return tuple(COMPILED_PARAMETER_NAMES)
    if not isinstance(parameter_names,(list,tuple)) or not parameter_names:
        raise ValueError('parameter names must be a nonempty list or tuple')
    names=tuple(parameter_names)
    if any(not isinstance(name,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name) or name=='time' for name in names):
        raise ValueError('parameter names must be scalar identifiers; time is reserved')
    if len(set(names))!=len(names):raise ValueError('duplicate runtime parameter name')
    return names

def _simulation_command(command,*,parameter_names=None,output_names=None):
    from .case_result_evidence import output_names as checked_output_names
    names=_parameter_names(parameter_names)
    outputs=checked_output_names(output_names,parameter_names=names)
    if any(str(arg).startswith(('-output=','-override=','-overrideFile=')) or arg in ('-output','-override','-overrideFile') for arg in command):
        raise ValueError('simulation command already contains an output or parameter override')
    return [*command,'-s=dassl','-output='+','.join(names+outputs)]

def _record_parameter_output(command,cwd,attempt,pid,task,result,*,parameter_names=None,output_names=None):
    """Bind captured stdout to the same owned launch and exact compiled source."""
    from .case_paths import save_json
    from .case_result_evidence import output_names as checked_output_names, runtime_file_bindings, RECEIPT_SCHEMA
    names=_parameter_names(parameter_names)
    outputs=checked_output_names(output_names,parameter_names=names)
    if [arg for arg in command if str(arg).startswith('-output=')]!=['-output='+','.join(names+outputs)]:
        raise ValueError('recorded parameter names differ from simulation command')
    path=Path(cwd)/'model.mo'
    # The inherited runner persists stdout with Path.write_text(newline=None).
    # Bind those exact bytes, including Windows newline translation, rather
    # than treating the decoded capture's LF bytes as the persisted log bytes.
    persisted_stdout=result['stdout'].replace('\n',os.linesep).encode('utf8')
    receipt=dict(schema='v12-owned-runtime-parameter-output-1',attempt_id=attempt,pid=pid,task_id=task,command=command,requested_parameters=list(names),source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),stdout_sha256=hashlib.sha256(persisted_stdout).hexdigest(),stdout_hash_basis='EXACT_INHERITED_RUNNER_UTF8_NATIVE_NEWLINE_FILE_BYTES',returncode=result['returncode'],timed_out=result['timed_out'],uncertain=result['uncertain'])
    if outputs:receipt.update(schema=RECEIPT_SCHEMA,requested_outputs=list(outputs),result_files=runtime_file_bindings(command,cwd,persisted_stdout))
    save_json(Path(cwd)/'runtime-parameter-output.json',receipt,immutable=True)

def _run(command,cwd,timeout):
    require_time();inside_write(cwd)
    task=os.environ.get('V12_CASE_TASK_ID')
    if not task:raise ValueError('tool launch requires owned current V12 task identity')
    kind='version' if '--version' in command else 'compile' if str(command[0]).lower().endswith('omc.exe') else 'simulation'
    names=_parameter_names(_execution_parameter_names.get())
    outputs=_execution_output_names.get()
    if kind=='simulation':
        if outputs:
            from .case_result_evidence import constant_boolean_metadata
            constant_boolean_metadata(Path(cwd)/'model_init.xml',outputs)
        command=_simulation_command(command,parameter_names=names,output_names=outputs)
    cap=20 if kind=='version' else authorization()['timeouts_s'][kind]
    timeout=min(float(cap),remaining())
    attempt=process_attempt(kind,dict(task_id=task,command=command,cwd=str(cwd),timeout_s=timeout))
    environment=dict(os.environ);environment['PATH']=str(_runner._OMC.parent)+os.pathsep+environment.get('PATH','')
    options=dict(cwd=str(cwd),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment)
    if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP
    else:options['start_new_session']=True
    started=time.monotonic()
    try:process=subprocess.Popen(command,**options)
    except OSError as exc:
        result=_runner._run_result(None,b'',b'',False,False,str(exc),started);finish_process(attempt,result);return result
    mark_process_launched(attempt,process.pid)
    emit_progress(kind,{'version':'正在核对实际编译器版本。','compile':'正在真实编译Modelica模型。','simulation':'正在真实运行仿真。'}[kind],task_id=task)
    try:
        stdout,stderr=process.communicate(timeout=timeout)
        result=_runner._run_result(process.returncode,stdout,stderr,False,False,'',started)
    except (subprocess.TimeoutExpired,KeyboardInterrupt) as exc:
        cleaned,error=_terminate_process_tree(process)
        try:stdout,stderr=process.communicate(timeout=5) if cleaned else (b'',b'')
        except subprocess.TimeoutExpired:cleaned=False;stdout,stderr=b'',b''
        result=_runner._run_result(process.poll(),stdout,stderr,isinstance(exc,subprocess.TimeoutExpired),not cleaned,'timeout' if isinstance(exc,subprocess.TimeoutExpired) else 'keyboard_interrupt',started,isinstance(exc,KeyboardInterrupt),cleaned,error)
    if kind=='simulation':_record_parameter_output(command,cwd,attempt,process.pid,task,result,parameter_names=names,output_names=outputs)
    finish_process(attempt,result)
    emit_progress(kind+'_complete',{'version':'编译器版本查询已返回。','compile':'编译进程已返回。','simulation':'仿真进程已返回。'}[kind],task_id=task)
    return result

def _build_mos(model_name,stop,intervals):
    return f'''loaded := loadModel(Modelica);
print("__V11_MSL_VERSION__=" + getVersion(Modelica) + "\\n");
loadedFile := loadFile("model.mo");
check := checkModel({model_name});
print("__V11_CHECK_MODEL__=" + check + "\\n");
build := buildModel({model_name}, startTime=0, stopTime={stop:.17g}, numberOfIntervals={intervals}, tolerance={_tolerance:.17g}, method="dassl", outputFormat="csv", fileNamePrefix="model");
print("__V11_BUILD_EXE__=" + build[1] + "\\n");
print("__V11_ERRORS__=" + getErrorString() + "\\n");
'''

_runner._run=_run
_runner._build_mos=_build_mos

def execute_model(source,model_name,out,stop,intervals=2400,simulation_timeout_s=900,*,parameter_names=None,output_names=None,confirmed_spec=None):
    """Internal caller's evidence names, never physical or budget authority.

    Ordinary production callers retain the exact default parameter contract.
    """
    from .case_asym2_family import is_asym2_source
    if is_asym2_source(source):
        from .case_asym2_execution import execute_asym2_model
        return execute_asym2_model(source,model_name,out,stop,intervals,simulation_timeout_s,confirmed_spec=confirmed_spec)
    global _tolerance
    from .case_scope import strip_comments,NUMBER,modelica_parameters
    from .case_result_evidence import output_names as checked_output_names
    names=_parameter_names(parameter_names)
    outputs=checked_output_names(output_names,parameter_names=names)
    declared=modelica_parameters(source)[0]
    missing_source=[name for name in names if name not in declared]
    if missing_source:raise ValueError('requested parameters absent from source declarations: '+','.join(missing_source))
    match=re.findall(r'\bTolerance\s*=\s*('+NUMBER+')',strip_comments(source))
    if len(match)!=1 or float(match[0]) not in (.01,.0001):raise ValueError('source tolerance must bind approved normal/refined setting')
    _tolerance=float(match[0])
    token=_execution_parameter_names.set(names)
    output_token=_execution_output_names.set(outputs)
    try:
        result=_runner.execute_model(source,model_name,out,stop,intervals,simulation_timeout_s=simulation_timeout_s)
        result['actual_run_settings']=dict(solver='dassl',tolerance=_tolerance,output_interval_s=stop/intervals,stop_time=stop,requested_by='explicit buildModel method/tolerance settings; source nested Euler annotation is not solver evidence')
        result['runtime_parameter_readback']=parameter_readback(source,out,parameter_names=names,output_names=outputs)
        if outputs:
            from .case_result_evidence import read_runtime_outputs
            try:
                evidence=read_runtime_outputs(source,out,parameter_names=names,names=outputs,stop_time=stop)
                result['runtime_constant_output_readback']={key:evidence[key] for key in ('status','outputs','time','output_line','compiled_constant_outputs','receipt_path','receipt_sha256','result_files')}
            except (OSError,ValueError,TypeError,KeyError) as exc:result['runtime_constant_output_readback']=dict(status='UNKNOWN',outputs={},issues=[str(exc)])
        experiment=result['runtime_parameter_readback'].get('compiled_default_experiment',{})
        result['actual_run_settings']['compiled_default_experiment']=experiment
        result['actual_run_settings']['solver_evidence']='simulation executable explicit -s=dassl'
        result['actual_run_settings']['tolerance_evidence']='compiled model_init.xml DefaultExperiment' if 'tolerance' in experiment else 'explicit buildModel tolerance; compiled readback unavailable'
        _runner._finish(result,out)
        return result
    finally:
        _execution_parameter_names.reset(token)
        _execution_output_names.reset(output_token)

def _runtime_parameter_values(source,out,expected,*,parameter_names=None,output_names=None):
    """Read OMC -output final values; never substitute source-derived expectations."""
    from .case_scope import NUMBER,strip_comments
    names=_parameter_names(parameter_names)
    from .case_result_evidence import output_names as checked_output_names
    outputs=checked_output_names(output_names,parameter_names=names)
    path=Path(out)/'simulation.stdout.log';binding=Path(out)/'runtime-parameter-output.json';model=Path(out)/'model.mo'
    result=dict(status='UNKNOWN',actual={},source=str(path),process_binding=str(binding),issues=[])
    if not path.is_file() or not binding.is_file():result['issues'].append('owned same-run stdout or process binding absent');return result
    try:
        if set(expected)!=set(names):raise ValueError('runtime parameter expectations differ from requested names')
        if outputs:
            from .case_result_evidence import read_runtime_outputs
            stop=re.findall(r'\bStopTime\s*=\s*('+NUMBER+')',strip_comments(source))
            if len(stop)!=1:raise ValueError('exact source-bound stop time required')
            evidence=read_runtime_outputs(source,out,parameter_names=names,names=outputs,stop_time=float(stop[0]))
            receipt=evidence['receipt']
            result.update(status='PASS',actual=evidence['parameters'],time=evidence['time'],stdout_sha256=receipt['stdout_sha256'],source_sha256=receipt['source_sha256'],process_attempt_id=receipt['attempt_id'],pid=receipt['pid'],task_id=receipt['task_id'],output_line=evidence['output_line'])
            return result
        receipt=json.loads(binding.read_text(encoding='utf8'));raw=path.read_bytes();source_hash=hashlib.sha256(source.encode('utf8')).hexdigest()
        if not isinstance(receipt,dict):raise ValueError('invalid runtime parameter-output receipt shape')
        if receipt.get('schema')!='v12-owned-runtime-parameter-output-1' or receipt.get('requested_parameters')!=list(names) or not receipt.get('attempt_id') or not receipt.get('task_id') or type(receipt.get('pid')) is not int or receipt['pid']<=0:raise ValueError('invalid owned parameter-output process receipt')
        if receipt.get('returncode')!=0 or receipt.get('timed_out') is not False or receipt.get('uncertain') is not False:raise ValueError('parameter stdout process did not complete successfully')
        if not model.is_file() or hashlib.sha256(model.read_bytes()).hexdigest()!=source_hash or receipt.get('source_sha256')!=source_hash:raise ValueError('runtime parameter source binding differs')
        if receipt.get('stdout_sha256')!=hashlib.sha256(raw).hexdigest():raise ValueError('runtime parameter stdout hash differs')
        command=receipt.get('command',[]);flags=[x for x in command if str(x).startswith('-output=')]
        if flags!=['-output='+','.join(names)] or any(str(x).startswith(('-override','-f=')) for x in command):raise ValueError('runtime parameter command binding differs')
        lines=[line.strip() for line in raw.decode('utf8').splitlines() if re.match(r'^\s*time\s*=',line)]
        if len(lines)!=1:raise ValueError('exactly one final runtime parameter output line required')
        values={}
        for item in lines[0].split(','):
            match=re.fullmatch(r'([A-Za-z_]\w*)\s*=\s*([^,\s]+)',item.strip())
            if not match or match[1] in values:raise ValueError('malformed or duplicate runtime parameter output')
            name,value=match.groups()
            if name not in {'time',*expected}:raise ValueError('unexpected runtime parameter output: '+name)
            numeric=float(value)
            if not math.isfinite(numeric):raise ValueError('nonfinite runtime parameter output: '+name)
            values[name]=numeric
        stop=re.findall(r'\bStopTime\s*=\s*('+NUMBER+')',strip_comments(source))
        if len(stop)!=1 or 'time' not in values or not math.isclose(values['time'],float(stop[0]),rel_tol=0,abs_tol=max(1e-12,abs(float(stop[0]))*1e-9)):raise ValueError('runtime parameter output is not at the source-bound final time')
        result.update(actual={k:v for k,v in values.items() if k!='time'},time=values['time'],stdout_sha256=receipt['stdout_sha256'],source_sha256=source_hash,process_attempt_id=receipt['attempt_id'],pid=receipt['pid'],task_id=receipt['task_id'],output_line=lines[0])
        result['status']='PASS' if set(result['actual'])==set(expected) else 'UNKNOWN'
        if result['status']!='PASS':result['issues'].append('runtime parameter output incomplete')
    except (OSError,UnicodeError,ValueError,TypeError,KeyError) as exc:result['issues'].append(str(exc))
    return result

def parameter_readback(source,out,*,parameter_names=None,output_names=None):
    from .case_scope import modelica_parameters
    import xml.etree.ElementTree as ET
    names=_parameter_names(parameter_names);declared=modelica_parameters(source)[0]
    expected={name:declared[name] for name in names if name in declared}
    missing_source=[name for name in names if name not in declared]
    if missing_source:return dict(status='UNKNOWN',expected=expected,actual={},requested_parameters=list(names),missing_source_parameters=missing_source,reason='requested parameters absent from source declarations',source='model.mo')
    path=Path(out)/'model_init.xml'
    if not path.is_file():return dict(status='UNKNOWN',expected=expected,actual={},reason='compiled init XML absent',source='model_init.xml')
    actual={};initial={};causality={};experiment={};issues=[]
    try:
        tree=ET.parse(path)
        default=tree.find('.//DefaultExperiment')
        if default is not None:experiment=dict(default.attrib)
        for node in tree.iter('ScalarVariable'):
            name=node.get('name')
            if name not in expected:continue
            if name in causality:raise ValueError('duplicate compiled parameter: '+name)
            causality[name]=node.get('causality','parameter')
            value=node.find('Real')
            if value is None:value=node.find('Integer')
            if value is not None and value.get('start') is not None:
                initial[name]=float(value.get('start'))
                if not math.isfinite(initial[name]):raise ValueError('nonfinite compiled parameter: '+name)
                if causality[name]!='calculatedParameter':actual[name]=initial[name]
    except (OSError,ET.ParseError,ValueError) as exc:return dict(status='UNKNOWN',expected=expected,actual=actual,reason=str(exc),source=str(path))
    runtime=_runtime_parameter_values(source,out,expected,parameter_names=names,output_names=output_names)
    direct={k for k in expected if causality.get(k)!='calculatedParameter'}
    mismatches=[k for k,v in actual.items() if not math.isclose(v,expected[k],rel_tol=1e-12,abs_tol=1e-12)]
    if runtime['status']=='PASS':
        for key,value in runtime['actual'].items():
            if not math.isclose(value,expected[key],rel_tol=1e-12,abs_tol=1e-12) or (key in actual and not math.isclose(value,actual[key],rel_tol=1e-12,abs_tol=1e-12)):mismatches.append(key)
        actual.update(runtime['actual'])
    missing_xml=sorted(direct-set(initial));missing_actual=sorted(set(expected)-set(actual))
    if missing_xml:issues.append('direct parameter XML initial values absent: '+','.join(missing_xml))
    if missing_actual:issues.append('evaluated parameter evidence absent: '+','.join(missing_actual))
    if mismatches:issues.append('source/XML/runtime parameter conflict: '+','.join(sorted(set(mismatches))))
    runtime_present=(Path(out)/'runtime-parameter-output.json').exists() or (Path(out)/'simulation.stdout.log').exists()
    if runtime_present and runtime['status']!='PASS':issues.extend(runtime['issues'])
    status='FAIL' if mismatches else 'UNKNOWN' if missing_xml or missing_actual or (runtime_present and runtime['status']!='PASS') else 'PASS'
    origins={key:('SIMULATION_FINAL_STDOUT_AND_COMPILED_XML_INITIAL' if key in direct and runtime['status']=='PASS' else 'SIMULATION_FINAL_STDOUT_EVALUATED_PARAMETER' if runtime['status']=='PASS' else 'COMPILED_XML_INITIAL') for key in actual}
    return dict(status=status,expected=expected,actual=actual,xml_initial_values=initial,xml_causality=causality,runtime_final_values=runtime['actual'],runtime_stdout_evidence=runtime,value_origins=origins,issues=issues,compiled_default_experiment=experiment,source=str(path),scope='COMPILED_XML_INITIAL_AND_SIMULATION_FINAL_PARAMETER_VALUES_NOT_HARDWARE_RATING')
