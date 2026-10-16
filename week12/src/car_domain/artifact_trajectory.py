"""Finite sampled, explicitly contracted task progress; never vehicle acceptance."""
from pathlib import Path
import csv, json, math
from week11.src.store import digest_json

SCHEMA = 'v11-artifact-trajectory-1'
CONTRACT_FIELDS = frozenset({'schema','route_semantics','entities','milestones','start_time','end_time','max_sample_gap','max_speed','position_tolerance','time_tolerance','coverage_note','model_basis','requirement_refs'})
CONTRACT_PROMPT = ''' For a finite forward route task, supply optional trajectory_contract, displayed for approval; never infer thresholds from done/zone or claim physical acceptance. Contract exactly: {schema:"'''+SCHEMA+'''",route_semantics:"ordered_forward_waypoints",entities:{entityName:{x:outputLabel,z:outputLabel}},milestones:[{id,phase,targets:{everyEntityName:{x_min,x_max,z_min,z_max}}}],start_time:0,end_time:settings.stop_time,max_sample_gap:positiveSeconds,max_speed:positiveMetresPerSecond,position_tolerance:nonnegativeMetres,time_tolerance:nonnegativeSecondsLessThanEndTime,coverage_note:nonemptyString,model_basis:exactNonemptyModelicaSourceSubstring,requirement_refs:[{source_id,source_text:exactNonemptyTrustedSubstring}]}. Only model_basis is omitted during design and added from the actual source during generation. outputs maps outputLabel to CSV variable, e.g. outputs.front_x="frontWheel.x"; entities.front.x must be "front_x", never "frontWheel.x". Include every represented wheel/axle in coverage_note; entities has 1..32 entries, milestones has 1..128 entries, unique nonempty id and nonempty phase. Every milestone covers exactly all tracked entities. All x/z output labels are distinct across entities and coordinates; after removing an optional model_name+"." prefix, their CSV variables must also be distinct (no aliases). Every target has exactly finite x_min,x_max,z_min,z_max with x_min<x_max and z_min<=z_max. For EACH entity, EACH later target must satisfy current.x_min > previous.x_max: overlap, touching edges and reverse order are rejected even when x_min increases. x_max-x_min must be strictly greater than 2*position_tolerance. Do not shrink, move or omit promised milestones to make a rejected confirmed candidate pass. start_time=0 and end_time=settings.stop_time; positive max_sample_gap/max_speed, nonnegative tolerances, time_tolerance<end_time. requirement_refs uses existing trusted source IDs and exact source substrings; model_basis must occur in frozen Modelica source. Explain model-chosen criteria, tolerances, continuity bounds, entity coverage and finite horizon as assumptions. Each entity follows its own ordered milestones, so front/rear may occupy different stages; one sample cannot complete multiple milestones. Initial position must precede first target and sampling must cover the horizon with the declared gap and speed bounds. Missing/unsupported criteria remain UNKNOWN. When modifying a baseline, preserve or explicitly revise its contract consistently; never silently weaken completion criteria.'''

class ProtocolValueError(ValueError):
    """Keep the original reason plus machine-readable, immutable-input diagnostics."""
    def __init__(self,message,path,actual,expected,**context):
        self.issue=dict(message=message,path=path,actual=diagnostic_value(actual),expected=expected,**{k:diagnostic_value(v) for k,v in context.items()})
        super().__init__(message+'; '+json.dumps({k:v for k,v in self.issue.items() if k!='message'},ensure_ascii=False,allow_nan=False,separators=(',',':')))

def diagnostic_value(value):
    if isinstance(value,float) and not math.isfinite(value):return str(value)
    if isinstance(value,dict):return {str(k):diagnostic_value(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [diagnostic_value(v) for v in value]
    return value if value is None or isinstance(value,(str,int,float,bool)) else repr(value)

def reject(message,path,actual,expected,**context):
    raise ProtocolValueError(message,path,actual,expected,**context)

def child_path(path,key):return path+'['+json.dumps(key,ensure_ascii=False)+']'

def validate_contract(contract, spec):
    from .model_artifact import finite
    root='$.trajectory_contract'
    if not isinstance(contract,dict):reject('unsupported explicit trajectory contract/route semantics',root,contract,'object with exactly '+str(sorted(CONTRACT_FIELDS)))
    if set(contract)!=CONTRACT_FIELDS:reject('unsupported explicit trajectory contract/route semantics',root,sorted(contract),'exact fields '+str(sorted(CONTRACT_FIELDS)))
    for key,expected in (('schema',SCHEMA),('route_semantics','ordered_forward_waypoints')):
        if contract[key]!=expected:reject('unsupported explicit trajectory contract/route semantics',root+'.'+key,contract[key],expected)
    for key in ('coverage_note','model_basis'):
        if not isinstance(contract[key],str) or not contract[key].strip():reject('trajectory interpretation/coverage basis required',root+'.'+key,contract[key],'nonempty string')
    if contract['model_basis'] not in spec['artifact']['modelica_source']:reject('trajectory model basis absent from frozen source',root+'.model_basis',contract['model_basis'],'exact substring of $.artifact.modelica_source')
    sources={s['id']:s['text'] for s in spec['sources']}
    refs=contract['requirement_refs']
    if not isinstance(refs,list) or not refs:reject('trajectory source-bound task required',root+'.requirement_refs',refs,'nonempty list of source_id/source_text objects')
    for index,ref in enumerate(refs):
        loc=root+'.requirement_refs['+str(index)+']'
        if not isinstance(ref,dict) or set(ref)!={'source_id','source_text'}:reject('trajectory trusted requirement reference mismatch',loc,ref,'exact fields source_id, source_text')
        if not isinstance(ref['source_id'],str) or ref['source_id'] not in sources:reject('trajectory trusted requirement reference mismatch',loc+'.source_id',ref['source_id'],'known trusted source ID',allowed_source_ids=list(sources))
        if not isinstance(ref['source_text'],str) or not ref['source_text'] or ref['source_text'] not in sources[ref['source_id']]:reject('trajectory trusted requirement reference mismatch',loc+'.source_text',ref['source_text'],'exact nonempty substring of trusted source '+ref['source_id'])
    for key in ('start_time','end_time','max_sample_gap','max_speed','position_tolerance','time_tolerance'):
        if not finite(contract[key]):reject('nonfinite trajectory criterion',root+'.'+key,contract[key],'finite number (not boolean)')
    for key,expected in (('start_time',0),('end_time',spec['settings']['stop_time'])):
        if contract[key]!=expected:reject('trajectory horizon/finite continuity criterion invalid',root+'.'+key,contract[key],expected)
    for key in ('max_sample_gap','max_speed','position_tolerance','time_tolerance'):
        if contract[key]<0 or (key in ('max_sample_gap','max_speed') and contract[key]==0):reject('trajectory horizon/finite continuity criterion invalid',root+'.'+key,contract[key],'> 0' if key in ('max_sample_gap','max_speed') else '>= 0')
    if contract['time_tolerance']>=contract['end_time']:reject('trajectory horizon/finite continuity criterion invalid',root+'.time_tolerance',contract['time_tolerance'],'< end_time',end_time=contract['end_time'])
    entities=contract['entities']
    if not isinstance(entities,dict) or not 1<=len(entities)<=32:reject('explicit tracked entities required',root+'.entities',entities,'object with 1..32 tracked entities')
    used=[];locations={};columns={};prefix=spec['artifact']['model_name']+'.'
    for name,mapping in entities.items():
        loc=child_path(root+'.entities',name)
        if not isinstance(name,str) or not name or not isinstance(mapping,dict) or set(mapping)!={'x','z'}:reject('invalid trajectory output mapping',loc,mapping,'nonempty entity name mapped to exactly x and z output labels')
        for axis,label in mapping.items():
            coordinate=loc+'.'+axis
            if not isinstance(label,str) or label not in spec['outputs']:reject('invalid trajectory output mapping',coordinate,label,'existing $.outputs label, not CSV variable',allowed_labels=list(spec['outputs']))
            used.append(label)
            if label in locations:reject('tracked entity coordinates must be distinct',coordinate,label,'distinct output label for every tracked coordinate',previous_path=locations[label])
            locations[label]=coordinate
    for label in used:
        column=spec['outputs'][label].removeprefix(prefix)
        if column in columns:reject('tracked entity coordinates cannot alias the same CSV variable',locations[label],label,'distinct CSV variable after optional model-name prefix removal',csv_variable=column,previous_label=columns[column])
        columns[column]=label
    milestones=contract['milestones'];ids=set();previous={}
    if not isinstance(milestones,list) or not 1<=len(milestones)<=128:reject('explicit ordered completion milestones required',root+'.milestones',milestones,'list of 1..128 ordered milestones')
    for index,stage in enumerate(milestones):
        loc=root+'.milestones['+str(index)+']'
        if not isinstance(stage,dict) or set(stage)!={'id','phase','targets'}:reject('invalid ordered trajectory milestone',loc,stage,'exact fields id, phase, targets')
        if not isinstance(stage['id'],str) or not stage['id'] or stage['id'] in ids:reject('invalid ordered trajectory milestone',loc+'.id',stage['id'],'unique nonempty string')
        if not isinstance(stage['phase'],str) or not stage['phase']:reject('invalid ordered trajectory milestone',loc+'.phase',stage['phase'],'nonempty string')
        ids.add(stage['id'])
        if not isinstance(stage['targets'],dict) or set(stage['targets'])!=set(entities):reject('every milestone must cover every tracked entity',loc+'.targets',list(stage['targets']) if isinstance(stage['targets'],dict) else stage['targets'],'exact entity names '+str(list(entities)))
        for name,box in stage['targets'].items():
            target=child_path(loc+'.targets',name)
            if not isinstance(box,dict) or set(box)!={'x_min','x_max','z_min','z_max'}:reject('invalid trajectory position/height range',target,box,'exact fields x_min,x_max,z_min,z_max')
            for key,val in box.items():
                if not finite(val):reject('invalid trajectory position/height range',target+'.'+key,val,'finite number (not boolean)')
            if box['x_min']>=box['x_max']:reject('invalid trajectory position/height range',target+'.x_min',box['x_min'],'< x_max',x_max=box['x_max'])
            if box['z_min']>box['z_max']:reject('invalid trajectory position/height range',target+'.z_min',box['z_min'],'<= z_max',z_max=box['z_max'])
            if box['x_max']-box['x_min']<=2*contract['position_tolerance']:reject('trajectory tolerance erases position range',target,box,'x_max-x_min > 2*position_tolerance',width=box['x_max']-box['x_min'],position_tolerance=contract['position_tolerance'],minimum_exclusive_width=2*contract['position_tolerance'])
            if name in previous and box['x_min']<=previous[name][0]:reject('forward milestone position ranges must be ordered and nonoverlapping',target+'.x_min',box['x_min'],'> previous x_max; overlap and touching edges rejected',previous_x_max=previous[name][0],previous_path=previous[name][1])
            previous[name]=(box['x_max'],target+'.x_max')

def evaluate_trajectory(spec, csv_path, mapping=None):
    """Evaluate each entity's ordered boxes from actual samples, without interpolation."""
    from .model_artifact import sha
    contract=spec.get('trajectory_contract')
    result=dict(schema='v11-artifact-trajectory-assessment-1',task_status='UNKNOWN',vehicle_task_status='UNKNOWN',physical_acceptance_status='NOT_VERIFIED',scope='FINITE_SAMPLED_ORDERED_TRAJECTORY_OBSERVATIONS_ONLY',milestones=[],reasons=[])
    if contract is None:
        result['reasons']=['NO_EXPLICIT_TRAJECTORY_COMPLETION_CONTRACT'];return result
    try:
        validate_contract(contract,spec)
        path=Path(csv_path);raw=path.read_bytes();result.update(csv_sha256=sha(raw),contract_sha256=digest_json(contract))
        with path.open(encoding='utf-8-sig',newline='') as handle:
            reader=csv.DictReader(handle);fields=reader.fieldnames or []
            if len(fields)!=len(set(fields)) or 'time' not in fields:raise ValueError('CSV_DUPLICATE_OR_MISSING_TIME_COLUMNS')
            prefix=spec['artifact']['model_name']+'.';resolved={}
            for label,requested in spec['outputs'].items():
                column=requested if requested in fields else requested[len(prefix):] if requested.startswith(prefix) and requested[len(prefix):] in fields else None
                if column is None:raise ValueError('CSV_MISSING_OUTPUT: '+requested)
                resolved[label]=column
            samples=[]
            for row in reader:
                if None in row or any(v is None for v in row.values()):raise ValueError('CSV_INCOMPLETE_ROW')
                values={k:float(v) for k,v in row.items()}
                if any(not math.isfinite(v) for v in values.values()):raise ValueError('CSV_NONFINITE')
                samples.append(values)
        if len(samples)<2:raise ValueError('CSV_INCOMPLETE_HORIZON')
        tol=contract['position_tolerance'];ttol=contract['time_tolerance']
        result['sample_evidence']=dict(rows=len(samples),start_time=samples[0]['time'],end_time=samples[-1]['time'],output_mapping=resolved)
        if abs(samples[0]['time']-contract['start_time'])>ttol or abs(samples[-1]['time']-contract['end_time'])>ttol:raise ValueError('CSV_INCOMPLETE_HORIZON')
        for prev,row in zip(samples,samples[1:]):
            dt=row['time']-prev['time']
            if dt<0 or dt>contract['max_sample_gap']+ttol:raise ValueError('CSV_TIME_ORDER_OR_SAMPLE_GAP')
            for entity in contract['entities'].values():
                x,z=(resolved[entity[k]] for k in ('x','z'))
                if math.hypot(row[x]-prev[x],row[z]-prev[z])>contract['max_speed']*dt+tol:raise ValueError('CSV_POSITION_DISCONTINUITY')
        # Each wheel/axle follows its own ordered route. A front axle may be
        # several steps ahead of the rear; no simultaneous same-step assumption.
        hits={name:[] for name in contract['entities']};indices={name:0 for name in hits}
        extrema={}
        for name,entity in contract['entities'].items():
            x,z=(resolved[entity[k]] for k in ('x','z'))
            extrema[name]=dict(min_x=min(r[x] for r in samples),max_x=max(r[x] for r in samples),final_x=samples[-1][x],final_z=samples[-1][z])
            hit_x=None
            for sample_index,row in enumerate(samples):
                index=indices[name]
                if index==len(contract['milestones']):break
                box=contract['milestones'][index]['targets'][name]
                if box['x_min']-tol<=row[x]<=box['x_max']+tol and box['z_min']-tol<=row[z]<=box['z_max']+tol:
                    if sample_index==0 or row[x]<=samples[sample_index-1][x] or (hit_x is not None and row[x]<=hit_x+tol):continue
                    if hits[name] and row['time']<=hits[name][-1]:continue
                    hits[name].append(row['time']);indices[name]+=1;hit_x=row[x]
        result['entity_evidence']=extrema
        # An already-beyond-route initial sample or a reversed trajectory cannot
        # establish forward passage merely by occupying a target box.
        for name,entity in contract['entities'].items():
            first=contract['milestones'][0]['targets'][name]
            if samples[0][resolved[entity['x']]]>=first['x_min']-tol:raise ValueError('FORWARD_ROUTE_ENTRY_NOT_OBSERVED')
            if hits[name] and extrema[name]['final_x']<samples[0][resolved[entity['x']]]-tol:raise ValueError('FORWARD_ROUTE_DIRECTION_NOT_OBSERVED')
        blocked=False
        for index,stage in enumerate(contract['milestones']):
            events={name:(times[index] if len(times)>index else None) for name,times in hits.items()}
            complete=all(t is not None for t in events.values())
            status='PASS' if complete else 'NOT_REACHED' if blocked else 'FAIL'
            result['milestones'].append(dict(id=stage['id'],phase=stage['phase'],status=status,entity_times=events,targets=stage['targets']))
            if not complete:blocked=True
        result['task_status']='FAIL' if blocked else 'PASS'
        result['reasons']=['ORDERED_ROUTE_NOT_COMPLETED_WITHIN_BOUND_HORIZON'] if blocked else ['ALL_TRACKED_ENTITIES_COMPLETED_ORDERED_ROUTE_IN_FINITE_SAMPLES']
        return result
    except (ValueError,TypeError,KeyError,OSError) as exc:
        result.update(task_status='UNKNOWN',evidence_status='ERROR',reasons=[str(exc)])
        return result

def supplemental_assessment(record, manifest, document):
    """Bound current read-only assessment; leave the recorded outcome untouched."""
    from .contracts import WEEK12_ROOT,inside_week12
    from .model_artifact import sha
    from copy import deepcopy
    if record['evidence_status']!='COMPLETE' or not record['recorded_conclusions_verified']:raise ValueError('supplemental trajectory requires complete historical evidence')
    base=inside_week12(WEEK12_ROOT/'projects'/record['project_id']/'versions'/record['version_id'])
    csv_path=base/'tool/model_res.csv'
    if 'tool/model_res.csv' not in manifest['files']:raise ValueError('manifest does not bind actual CSV')
    binding=dict(task_id=record['task_id'],project_id=record['project_id'],version_id=record['version_id'],spec_digest=record['spec_digest'],source_sha256=record['source_sha256'],manifest_sha256=record['manifest_sha256'],csv_sha256=sha(csv_path.read_bytes()),spec_file_sha256=sha((base/'spec.json').read_bytes()))
    if not isinstance(document,dict) or set(document)!={'schema','binding','contract','interpretation_note'} or document['schema']!='v11-supplemental-trajectory-1' or document['binding']!=binding:raise ValueError('supplemental trajectory evidence binding drift')
    if not isinstance(document['interpretation_note'],str) or not document['interpretation_note'].strip():raise ValueError('supplemental interpretation note required')
    spec=deepcopy(record['frozen_spec']);spec['trajectory_contract']=document['contract']
    validate_contract(document['contract'],spec)
    if record.get('tool_status')!='PASS' or record.get('chain_status')!='PASS':
        assessed=dict(schema='v11-artifact-trajectory-assessment-1',task_status='UNKNOWN',vehicle_task_status='UNKNOWN',physical_acceptance_status='NOT_VERIFIED',milestones=[],reasons=['ORIGINAL_TOOL_OR_CHAIN_NOT_PASS'])
    else:assessed=evaluate_trajectory(spec,csv_path)
    return dict(assessment_kind='SUPPLEMENTAL_CURRENT_READ_ONLY_NOT_ORIGINAL_APPROVAL',binding=binding,document_sha256=digest_json(document),interpretation_note=document['interpretation_note'],recorded_task_status=record['task_status'],assessment=assessed,read_only=True,published=False)
