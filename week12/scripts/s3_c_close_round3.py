"""Append the actual finite-batch closeout without rewriting prior evidence."""
import math
from week12.src.integration.execution import OUT,ROOT,Batch,read,save,sha,require_sources
from week12.src.integration.timing_amendment import inputs,RECEIPT,SPEC


def main():
    inputs()
    require_sources(read(OUT/'round-03/source-manifest.json'))
    authorization=read(RECEIPT)
    if sha(OUT/'authorization.json')!=authorization['previous_authorization_sha256'] or sha(OUT/'checkpoint-result.json')!=authorization['previous_checkpoint_sha256']:
        raise ValueError('HISTORICAL_AUTHORIZATION_OR_CHECKPOINT_DRIFT')
    reports=[read(OUT/f'round-{n:02d}/batch-result.json') for n in (1,2,3)]
    scenes=[s for report in reports for s in report['scenes']]
    flat=read(OUT/'round-03/fixed_nose_flat_brake-base/physical.json')
    flat_refined=read(OUT/'round-03/fixed_nose_flat_brake-refined/physical.json')
    numeric=read(OUT/'round-03/fixed_nose_flat_brake-refined/numerical.json')
    stairs=read(OUT/'round-03/fixed_nose_three_up-base/physical.json')
    details=read(OUT/'round-03/stair-diagnosis-details.json')
    event=read(OUT/'round-03/event-row-diagnosis.json')
    usage=Batch().usage()
    classification={'classification':'physical_task_failure_and_unresolved_contact_event_discrepancy',
        'specification_digest':SPEC,'automatic_retry_allowed':False,
        'confirmed':[
            'At8.711s the approved nose-drive envelope overlaps the first step by8.955mm; allowance0.1mm.',
            'Vehicle stalls near first/second steps; rear pair torque saturates60Nm, front pair sustains slip, nose pair has no load in the terminal pose.',
            'Continuous supported high slip22.722s exceeds2s; no complete exit or cruise.'],
        'contact_event_evidence':{
            'event_time_s':10.33670789436808,'overlap_m':.0026312270386701087,
            'observed_zero_contact_interval_s':10.33670789460325-10.33670789436808,
            'rows_exceeding_geometry_tolerance_in_local_window':event['bad_rows'],
            'classification':'localized at convex-corner/upper-face transition; root cause and refined behavior not established',
            'event_rows_retained':True,'not_concluded':'not proof that this tiny transition interval caused the later stall'},
        'budget_stop':'All3 rounds,24 local checks and3 SysML slots consumed; remaining scene slots do not grant another round.',
        'next_work':'Read-only design/contact review using current evidence; prepare a concrete corrected scope and finite budget before new execution. Do not tune parameters, alter B or thresholds, or start lower-priority stair cases silently.'}
    save(OUT/'round-03/diagnosis.json',classification)
    result={'status':'STOPPED_THREE_STEP_ASCENT_FAILED_C_INCOMPLETE','specification_digest':SPEC,
        'usage':usage,'rounds_used':3,'C_local_cumulative':54,'C_local_passes':53,'C_historical_local_timeouts':1,
        'execution_local_passes':24,'sysml_passes':3,'product_online_calls':0,
        'scenes_passed':sum(s['status']=='PASS' for s in scenes),
        'modelica_compiles_passed':sum(s['compile']=='PASS' for s in scenes),
        'simulations_started':sum(s['simulation']!='NOT_RUN' for s in scenes),
        'simulations_completed':sum(s['simulation']=='COMPLETED' for s in scenes),
        'flat_brake':{'base':flat['status'],'refined':flat_refined['status'],'numeric':numeric['status'],
                      'settled_force_residual_N':flat['metrics']['settled_force_residual_max_N']},
        'three_up':{'compile':'PASS','simulation':'COMPLETED','physical':stairs['status'],'numeric':'NOT_RUN',
                    'failed_checks':[c for c in stairs['checks'] if not c['pass']],
                    'worst_nonwheel_overlap':details['worst_nonwheel_overlap'],
                    'terminal_pitch_deg':math.degrees(details['snapshots']['35']['chassisPitch'])},
        'not_run_in_last_round':reports[-1]['not_run'],
        'remaining_executable_rounds':0,
        'protection':'approved original/amended input hashes, historic authorization/checkpoint and current round3 source manifest unchanged',
        'evidence':{'batch':'round-03/batch-result.json','diagnosis':'round-03/diagnosis.json',
                    'details':'round-03/stair-diagnosis-details.json','event':'round-03/event-row-diagnosis.json'},
        'not_complete':['C','S3','V11'],'not_entered':['second structure','D','E','vehicle tuning','capability scan','V12']}
    save(OUT/'final-result.json',result)
    paths=[OUT/'final-result.json',OUT/'round-03/diagnosis.json',OUT/'round-03/stair-diagnosis-details.json',
        OUT/'round-03/event-row-diagnosis.json',RECEIPT,ROOT/'scripts/s3_c_diagnose_stairs.py',
        ROOT/'scripts/s3_c_diagnose_event_rows.py',ROOT/'scripts/s3_c_close_round3.py']
    save(OUT/'closeout-manifest.json',{'files':[{'path':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in paths]})
    print(result['status'],usage,'compiles',result['modelica_compiles_passed'],'simulations',result['simulations_completed'])


if __name__=='__main__': main()
