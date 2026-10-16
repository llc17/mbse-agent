"""Preserve the actual C stop and a separate, unapproved timing amendment."""
from copy import deepcopy
from pathlib import Path
from week12.src.integration.execution import OUT, ROOT, Batch, read, save, sha, approved_inputs
from week12.src.integration.review import review_identity
from week12.src.composition.schema import digest


def main():
    packet,bundle,contract=approved_inputs()
    rounds=[read(OUT/f'round-{n:02d}'/'batch-result.json') for n in (1,2)]
    scenes=[s for r in rounds for s in r['scenes']]
    physical=read(OUT/'round-02/fixed_nose_flat_brake-base/physical.json')
    usage=Batch().usage()
    report={'status':'STOPPED_PHYSICAL_SETTLING_GATE_NOT_C_COMPLETE',
            'specification_digest':packet['confirmation_identity']['specification_digest'],
            'usage':usage,'rounds_used':2,'modelica_compile_passes':sum(s['compile']=='PASS' for s in scenes),
            'simulations_started':sum(s['simulation']!='NOT_RUN' for s in scenes),
            'simulations_completed':sum(s['simulation']=='COMPLETED' for s in scenes),
            'scene_passes':sum(s['status']=='PASS' for s in scenes),
            'C_local_cumulative':46,'C_local_cumulative_pass':45,'C_local_historical_timeout':1,
            'execution_local_passes':16,'sysml_passes':2,'identity_probes':0,'product_online_calls':0,
            'vehicle_case':{'case':'fixed_nose_flat_brake','variant':'base','compile':'PASS','simulation':'COMPLETED',
                'physical':'FAIL','checks_passed':sum(c['pass'] for c in physical['checks']),
                'checks_total':len(physical['checks']),'failed':[c for c in physical['checks'] if not c['pass']],
                'numeric':'NOT_RUN'},
            'diagnosis':'round-02/settling-diagnosis.json',
            'remaining_execution_slots':{'scene':54-usage['scene'],'local':24-usage['local'],'sysml':3-usage['sysml'],
                'rounds':1,'wall_seconds':7200-usage['process_seconds']},
            'remaining_slots_are_not_authorization_to_bypass_physical_failure':True,
            'protected_approved_inputs':'UNCHANGED_RECHECKED',
            'subagent_delivery':'Both Sol/High jobs exited with quota errors; partial files preserved, main completed and reviewed implementations.',
            'not_claimed':['vehicle numerical pass','stairs','flat steady energy','serial mission','second structure','C complete','D/E','V12']}
    save(OUT/'checkpoint-result.json',report)
    save(OUT/'round-02/diagnosis.json',{
        'classification':'physical_precondition_failure','evidence':'settling-diagnosis.json',
        'failure':'Residual settling at t2 violates frozen2-3s static balance window; independently corroborated by contact law and mass acceleration.',
        'automatic_implementation_retry_allowed':False,
        'next_action':'Review a timing amendment; original scenario, failed result and thresholds are not rewritten.'})
    target=OUT/'timing-amendment-draft'; target.mkdir(exist_ok=False)
    b=deepcopy(bundle); c=deepcopy(contract)
    b['physical']['controller']['settle_until_s']=4
    b['physical']['initialization']['note']='Source 10mm release; body reference changed by explicit mass layout; proposed4s free settling, no pose servo'
    for case in b['physical']['cases']:
        case['reference_speed_table_s_m_s'][1][0]=4
        case['reference_speed_table_s_m_s'][2][0]=6
    b['physical'].pop('physical_digest')
    b['physical']['physical_digest']=digest(b['physical'])
    c['physical']['settled_window_s']=[3,4]
    c['physical']['initialization_reference']='10mm nominal release; free settling0-4s; ramp4-6s; no initial pose or vehicle velocity imposed after t0'
    c['physical']['stagnation_scope']='stair traversal after t=6 and before exit; flat_brake stop excluded'
    # Retain the original cumulative ceiling, history, and remaining last round.
    c['budget_proposal']['note']='DRAFT amendment inside remaining original totals, not a reset: rounds1-2 consumed10 scene/16 local/2 SysML; only round3 remains, at most18 scene/8 local/1 SysML, zero additional repair rounds; history and7200s total retained.'
    c['scope']='one user fixed-nose vehicle; proposed1s additional settling; original case order and all physical/numerical tolerances retained'
    save(target/'proposal.json',b); save(target/'verification-contract.json',c)
    identity=review_identity([b],c)
    save(target/'review-identity.json',identity)
    def changes(a,b,path=''):
        if isinstance(a,dict) and isinstance(b,dict):
            for key in sorted(set(a)|set(b)):
                yield from changes(a.get(key),b.get(key),path+'/'+key)
        elif isinstance(a,list) and isinstance(b,list) and len(a)==len(b):
            for i,(x,y) in enumerate(zip(a,b)): yield from changes(x,y,path+'/'+str(i))
        elif a!=b: yield {'path':path,'before':a,'after':b}
    save(target/'changes.json',{'status':'DRAFT_NOT_APPROVED_NOT_EXECUTABLE',
         'old_specification_digest':packet['confirmation_identity']['specification_digest'],
         'new_specification_digest':identity['specification_digest'],
         'proposal_changes':list(changes(bundle,b)),'contract_changes':list(changes(contract,c)),
         'implementation_required_after_approval':['Bind real user amendment receipt, preserve old identities and global ledger',
          'Update source identity checks to the approved revision without permitting arbitrary specifications',
          'Derive all settling/support/stagnation timing from revised contract, not old hardcoded2-3/5s',
          'Run remaining complete round with8 preflight/1 SysML/at most18 scene slots; no extension beyond original totals'],
         'no_simulation_of_draft':True,'no_user_approval_created':True})
    summary='''# C 静置时间修订草案（待用户确认，未执行）

原方案已生成并完成20秒平地仿真，唯一不通过项：2–3秒静载检查最大力偏差23.036N，高于16.677N（车重2%）上限。原始接触力与独立接触公式相差小于3e-12N，合外力与质量×加速度的差最大0.0153N；这是落地余振，不是漏算质量。2.75–2.999秒的峰值已降至3.617N，支持延长等待的建议，但不证明新方案已经通过。

| 项目 | 已执行方案 | 本草案 |
|---|---|---|
| 零驱动力静置 | 0–3秒 | 0–4秒 |
| 静载检查窗口 | 2–3秒 | 3–4秒 |
| 加速到0.4m/s | 3–5秒 | 4–6秒 |
| 楼梯停滞检查起点 | 第5秒 | 第6秒 |
| 静载力/速度/力矩及其余容差 | 原冻结值 | 全部保持 |

85kg、质量分布、几何、接触刚度/阻尼、滚阻、驱动、电源、10mm释放均不变。制动仍12–14秒减速、18–20秒检查；平地能量仍15–35秒，七个工况终止时间不变。独立下楼的顶部参考使用新的3–4秒静置窗口；串联下楼仍使用实时平台支撑历史。

预算不清零、不追加：原上限54场景/24本地/3SysML/3轮/7200执行秒；已用10/16/2/2轮/89.576秒。若确认，本草案最多使用剩余第3轮18场景、8本地、1SysML，产品在线0，没有额外修复轮次。出错先诊断；物理失败或剩余预算不足则不能继续执行。

完整待审输入为同目录proposal.json、verification-contract.json；changes.json逐字段列出差异，review-identity.json绑定新规格。旧输入、C协议和历史结果均保持。该目录没有用户批准回执，也没有新方案运行证据。
'''
    (target/'review.md').write_text(summary,encoding='utf-8')
    save(target/'manifest.json',{'status':'DRAFT_NOT_APPROVED','files':[
        {'name':p.name,'sha256':sha(p)} for p in sorted(target.iterdir()) if p.is_file()]})
    print(report['status'],usage,identity['specification_digest'])


if __name__=='__main__': main()
