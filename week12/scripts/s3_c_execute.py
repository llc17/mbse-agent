"""Approved bounded C execution driver. Every actual tool call is reserved."""
import argparse
from pathlib import Path
import os
import sys
import unittest

from week12.src.integration.execution import (ROOT, OUT, REVIEW, APPROVED_SPEC, Batch,
    read,save,sha,approved_inputs,initialize,freeze_sources,require_sources,dependency_audit,execute_scene)
from week12.src.integration.review import check_review_receipt


def test_targets():
    def flatten(suite):
        for item in suite:
            if isinstance(item,unittest.TestSuite): yield from flatten(item)
            else: yield item.id()
    names=[]
    for module in ('week12.tests.test_c_model_generator','week12.tests.test_c_vehicle_verify','week12.tests.test_c_execution'):
        names.extend(flatten(unittest.defaultTestLoader.loadTestsFromName(module)))
    if len(names)!=8 or any('_FailedTest' in n for n in names):
        raise RuntimeError('EXPECTED_EIGHT_EXPLICIT_PREFLIGHT_TESTS: '+str(names))
    return names


def run_round(no):
    if no==3:
        from week12.src.integration.timing_amendment import inputs
        packet,bundle,contract=inputs()
    else:
        packet,bundle,contract=approved_inputs()
        check_review_receipt(read(OUT/'authorization.json'),packet['confirmation_identity'])
        if read(OUT/'proposal.json')!=bundle or read(OUT/'verification-contract.json')!=contract:
            raise RuntimeError('EXECUTION_COPY_DRIFT')
    spec=packet['confirmation_identity']['specification_digest']
    batch=Batch(specification_digest=spec)
    if not 1<=no<=3: raise RuntimeError('ROUND_LIMIT')
    if no>1:
        previous=OUT/f'round-{no-1:02d}'
        diagnosis=read(previous/'diagnosis.json')
        amended=(no==3 and diagnosis.get('classification')=='physical_precondition_failure')
        if not amended and (diagnosis.get('classification')!='implementation' or not diagnosis.get('repair_description')):
            raise RuntimeError('NO_AUTHORIZED_IMPLEMENTATION_REPAIR_DIAGNOSIS')
    if batch.usage()['scene']+18>54 or batch.usage()['local']+8>24:
        raise RuntimeError('INSUFFICIENT_BUDGET_FOR_COMPLETE_AFFECTED_SET')
    lock=OUT/'.execution.lock'
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    os.write(fd,str(os.getpid()).encode('ascii')); os.close(fd)
    directory=OUT/f'round-{no:02d}'
    report={'status':'IMPLEMENTATION_PREFLIGHT','round':no,'local':[],'scenes':[],
            'specification_digest':spec,'online':0}
    owns=False
    try:
        directory.mkdir(exist_ok=False); owns=True
        save(directory/'proposal.json',bundle)
        save(directory/'verification-contract.json',contract)
        save(directory/'confirmation-identity.json',packet['confirmation_identity'])
        manifest=freeze_sources(directory)
        if no>1:
            old=read(OUT/f'round-{no-1:02d}'/'source-manifest.json')
            if old==manifest: raise RuntimeError('UNCHANGED_SOURCE_BLIND_RETRY_REJECTED')
        dependency_audit(directory)
        for index,target in enumerate(test_targets(),1):
            batch.reserve('local',target,no)
            proc=batch.run([sys.executable,'-B','-m','unittest',target],ROOT.parent,10,directory,f'local-{index:02d}')
            status='PASS' if proc['returncode']==0 and not proc['timed_out'] else 'FAIL'
            report['local'].append({'test':target,'status':status})
            print(target,status,flush=True)
            if status!='PASS': raise RuntimeError('LOCAL_PREFLIGHT_FAILURE')
        require_sources(manifest)
        from week12.src.integration.model_generator import generate
        generated=generate(bundle,'fixed_nose_flat_brake')
        (directory/'vehicle.sysml').write_text(generated['sysml'],encoding='utf-8',newline='\n')
        save(directory/'sysml-trace.json',generated['trace'])
        batch.reserve('sysml','single_vehicle_source',no)
        proc=batch.run([sys.executable,'-B','-m','week12.scripts.s3_c_execute',
                        '--parse-sysml',str(directory/'vehicle.sysml'),'--parse-output',str(directory/'sysml-result.json')],
                       ROOT.parent,30,directory,'sysml')
        if proc['returncode']!=0 or proc['timed_out']: raise RuntimeError('SYSML_PARSER_FAILURE')
        sysml=read(directory/'sysml-result.json')
        report['sysml']=sysml.get('status','UNVERIFIED')
        if report['sysml'].upper()!='PASS': raise RuntimeError('SYSML_SYNTAX_FAILED')
        if sysml.get('tool_version')!='0.34.1': raise RuntimeError('SYSML_PARSER_IDENTITY_DRIFT')
        for case_id in contract['case_order']:
            base=None
            for refined in (False,True):
                result,path=execute_scene(batch,directory,bundle,contract,case_id,refined,no,manifest,base)
                report['scenes'].append(result)
                if result['status']!='PASS': raise RuntimeError('SCENE_STOP_FOR_DIAGNOSIS')
                if not refined: base=path
        report['status']='PASS_APPROVED_SINGLE_VEHICLE_NOT_ALL_C'
    except BaseException as exc:
        report['status']='PAUSED_REQUIRES_FAILURE_CLASSIFICATION'
        report['error']=type(exc).__name__+': '+str(exc)
    finally:
        report['usage']=batch.usage()
        report['not_run']=[c+('-refined' if r else '-base') for c in contract['case_order'] for r in (False,True)
                           if not any(s['case']==c and s['variant']==('refined' if r else 'base') for s in report['scenes'])]
        if owns: save(directory/'batch-result.json',report)
        # Remove only this owned lock file, never any prior lock or directory.
        if lock.is_file() and lock.read_text(encoding='ascii')==str(os.getpid()): lock.unlink()
    print(report['status'],report.get('error',''),report['usage'],flush=True)
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--initialize',action='store_true')
    parser.add_argument('--round',type=int)
    parser.add_argument('--parse-sysml'); parser.add_argument('--parse-output')
    args=parser.parse_args()
    if args.initialize:
        initialize(); print('APPROVAL_BOUND_IMPLEMENTATION_MAY_PROCEED')
    elif args.parse_sysml:
        from week12.src.toolchain import check_sysml
        result=check_sysml(Path(args.parse_sysml).read_text(encoding='utf-8'))
        save(Path(args.parse_output),result)
        print(result.get('status'),flush=True)
        if result.get('status','').upper()!='PASS': raise SystemExit(1)
    elif args.round:
        result=run_round(args.round)
        if not result['status'].startswith('PASS_'): raise SystemExit(1)
    else: parser.error('explicit initialize, round or parser subcommand required')


if __name__=='__main__': main()
