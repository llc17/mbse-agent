"""CSV-bound basic curves; standard-library verification, lazy raster rendering."""
from pathlib import Path
import csv, hashlib, json, math
from week11.src.store import digest_json

SCHEMA='v11-artifact-delivery-1'
CSV_NAME='tool/model_res.csv'

class SignalEvidenceError(ValueError):
    def __init__(self,message,*,mapping=None,missing=None):
        super().__init__(message);self.mapping=mapping or {};self.missing=missing or []

def file_record(path):
    path=Path(path)
    return dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),bytes=path.stat().st_size)

def read_signals(spec,path):
    """Keep actual sample order, including repeated event times; never interpolate."""
    mapping={};times=[];series={label:[] for label in spec['outputs']}
    try:
        with Path(path).open(encoding='utf-8-sig',newline='') as handle:
            reader=csv.DictReader(handle);fields=reader.fieldnames or []
            if len(fields)!=len(set(fields)):raise SignalEvidenceError('duplicate CSV columns')
            prefix=spec['artifact']['model_name']+'.';missing=[]
            for label,requested in spec['outputs'].items():
                resolved=requested if requested in fields else requested[len(prefix):] if requested.startswith(prefix) and requested[len(prefix):] in fields else None
                mapping[label]=dict(requested=requested,resolved=resolved)
                if resolved is None:missing.append(requested)
            if missing:raise SignalEvidenceError('missing output columns',mapping=mapping,missing=sorted(set(missing)))
            if 'time' not in fields:raise SignalEvidenceError('missing time column',mapping=mapping)
            for index,row in enumerate(reader,2):
                if None in row or any(value is None for value in row.values()):raise SignalEvidenceError('malformed CSV row '+str(index),mapping=mapping)
                try:
                    t=float(row['time']);values={label:float(row[item['resolved']]) for label,item in mapping.items()}
                except (ValueError,TypeError,KeyError) as exc:raise SignalEvidenceError('invalid numeric sample at row '+str(index),mapping=mapping) from exc
                if not math.isfinite(t) or any(not math.isfinite(v) for v in values.values()):raise SignalEvidenceError('nonfinite CSV sample at row '+str(index),mapping=mapping)
                if times and t<times[-1]:raise SignalEvidenceError('decreasing CSV time at row '+str(index),mapping=mapping)
                times.append(t)
                for label,value in values.items():series[label].append(value)
    except (OSError,TypeError,csv.Error,UnicodeError) as exc:raise SignalEvidenceError('CSV unreadable: '+str(exc),mapping=mapping) from exc
    if not times:raise SignalEvidenceError('empty CSV samples',mapping=mapping)
    return dict(mapping=mapping,times=times,series=series,summary=dict(rows=len(times),time_start=times[0],time_end=times[-1],duplicate_times=sum(a==b for a,b in zip(times,times[1:])),final_values={label:values[-1] for label,values in series.items()}))

def render_curves(spec,samples,root,binding,*,test_only=False):
    # No graph-library initialization in history, CLI, or module imports.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    font_path=Path('C:/Windows/Fonts/msyh.ttc')
    if font_path.is_file():
        from matplotlib import font_manager
        font_manager.fontManager.addfont(str(font_path))
        plt.rcParams['font.family']=font_manager.FontProperties(fname=str(font_path)).get_name()
    plt.rcParams['axes.unicode_minus']=False
    labels=list(samples['series']);records=[];folder=Path(root)/'curves';folder.mkdir(exist_ok=True)
    for start in range(0,len(labels),4):
        group=labels[start:start+4];fig,axes=plt.subplots(len(group),1,figsize=(10.5,2.5*len(group)+1),squeeze=False,sharex=True)
        try:
            for axis,label in zip(axes[:,0],group):
                column=samples['mapping'][label]['resolved'];values=samples['series'][label]
                axis.plot(samples['times'],values,lw=1.4);axis.set_ylabel(label+'\n'+column)
                axis.set_title('Final '+label+' = '+format(values[-1],'.8g'),loc='left',fontsize=10);axis.grid(alpha=.25)
            axes[-1,0].set_xlabel('time (CSV)')
            fig.suptitle(('TEST_ONLY | ' if test_only else '')+spec['artifact']['model_name']+' | '+binding['task_id'],fontsize=11)
            fig.text(.02,.012,'Raw CSV samples; output units are not inferred; no vehicle acceptance claim.',fontsize=8)
            fig.tight_layout(rect=(0,.035,1,.94));path=folder/('signals-'+str(start//4+1).zfill(2)+'.png')
            fig.savefig(path,dpi=140)
            records.append(dict(path=path.relative_to(root).as_posix(),outputs=group,**file_record(path)))
        finally:plt.close(fig)
    return records

def create_delivery(spec,csv_path,root,binding,outcome,*,source_root=None,scope='TASK_RESULT'):
    root=Path(root);source_root=Path(source_root or root)
    identity={k:binding[k] for k in ('task_id','project_id','version_id','spec_digest','source_sha256')}
    if identity['spec_digest']!=digest_json(spec):raise ValueError('delivery spec digest mismatch')
    report=dict(schema=SCHEMA,binding=identity,scope=scope,test_only=bool(outcome.get('test_only')),data_status='NOT_RUN',plot_status='NOT_RUN',plots=[],reasons=[])
    if outcome.get('tool_status')=='PASS':
        try:
            samples=read_signals(spec,csv_path)
            report.update(data_status='PASS',csv=dict(path=Path(csv_path).relative_to(source_root).as_posix(),**file_record(csv_path)),output_mapping=samples['mapping'],samples=samples['summary'])
        except (SignalEvidenceError,ValueError) as exc:report.update(data_status='INCOMPLETE',plot_status='NOT_RUN',reasons=[str(exc)])
        else:
            try:report['plots']=render_curves(spec,samples,root,identity,test_only=report['test_only']);report['plot_status']='PASS'
            except Exception as exc:report.update(plot_status='FAILED',reasons=['curve rendering failed: '+type(exc).__name__+': '+str(exc)])
    report['status']='PASS' if report['data_status']==report['plot_status']=='PASS' else 'INCOMPLETE' if report['data_status']=='INCOMPLETE' else 'FAILED' if report['plot_status']=='FAILED' else 'NOT_RUN'
    (root/'delivery.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf8')
    return dict(contract=SCHEMA,report='delivery.json',status=report['status'],data_status=report['data_status'],plot_status=report['plot_status'],scope=scope)

def verify_delivery(spec,outcome,manifest,root,identity):
    """Verify frozen graph/data binding only; never render while reading history."""
    summary=outcome.get('result_delivery')
    if summary is None:return dict(status='HISTORICAL_NOT_GENERATED',data_status='UNKNOWN',plot_status='NOT_GENERATED',scope='HISTORICAL_RECORD'),[]
    if summary.get('contract')!=SCHEMA or summary.get('report')!='delivery.json' or 'delivery.json' not in manifest['files']:raise ValueError('delivery report missing or unsupported')
    report=json.loads((Path(root)/'delivery.json').read_bytes())
    expected={k:identity[k] for k in ('task_id','project_id','version_id','spec_digest','source_sha256')}
    if report.get('schema')!=SCHEMA or report.get('binding')!=expected:raise ValueError('delivery task/spec/source identity mismatch')
    if summary!=dict(contract=SCHEMA,report='delivery.json',status=report.get('status'),data_status=report.get('data_status'),plot_status=report.get('plot_status'),scope=report.get('scope')):raise ValueError('delivery summary/report drift')
    if report.get('test_only')!=bool(outcome.get('test_only')) or report.get('scope')!='TASK_RESULT':raise ValueError('delivery mode/scope mismatch')
    if report['data_status']=='PASS':
        if report.get('csv')!=dict(path=CSV_NAME,**manifest['files'].get(CSV_NAME,{})):raise ValueError('delivery CSV hash binding mismatch')
        samples=read_signals(spec,Path(root)/CSV_NAME)
        if report.get('samples')!=samples['summary'] or report.get('output_mapping')!=samples['mapping']:raise ValueError('delivery CSV sample/mapping drift')
    plots=report.get('plots',[]);paths=[];covered=[]
    for item in plots:
        name=item.get('path')
        if not isinstance(name,str) or not name.startswith('curves/') or name not in manifest['files'] or manifest['files'][name]!=dict(sha256=item.get('sha256'),bytes=item.get('bytes')):raise ValueError('delivery plot hash/path binding mismatch')
        covered.extend(item.get('outputs',[]));paths.append(str(Path(root)/name))
    if report['plot_status']=='PASS' and (report['data_status']!='PASS' or covered!=list(spec['outputs']) or not plots):raise ValueError('curve PASS lacks mapped samples/plots')
    if report['status']=='PASS' and (report['data_status']!='PASS' or report['plot_status']!='PASS'):raise ValueError('delivery PASS lacks data/curves')
    return dict(summary,reasons=report.get('reasons',[])),paths
