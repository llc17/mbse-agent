"""Offline handoff for the frozen eight-wheel backend; grants no execution rights."""
from pathlib import Path
import hashlib
import sys
from .contracts import PHYSICAL_PATH, PHYSICAL_SHA, REPO_ROOT
from .outcomes import SCENE_ORDER


def verification_command(directory, manifest, graph, scene, base=None, *, numerical_contract=None):
    if scene not in SCENE_ORDER:raise ValueError('unsupported vehicle scene')
    directory=Path(directory)
    command=[sys.executable,'-X','utf8','-B','-m','week12.scripts.s3_c_active_cluster','verify-child',
        '--source',str(directory/'result.csv'),'--manifest',str(manifest),'--graph',str(graph),
        '--scene',scene,'--report',str(directory/'independent-result.json')]
    if base is not None:command+=['--base',str(Path(base)/'result.csv')]
    if numerical_contract is not None:command += ['--numerical-contract',str(numerical_contract)]
    return command


def fixed_scene_plan(spec):
    if hashlib.sha256((REPO_ROOT/PHYSICAL_PATH).read_bytes()).hexdigest()!=PHYSICAL_SHA:
        raise ValueError('frozen vehicle specification changed')
    import json
    if spec!=json.loads((REPO_ROOT/PHYSICAL_PATH).read_bytes()):raise ValueError('unapproved physical change')
    numerical=spec['execution_contract_proposal']['numerical']
    limits={s['name']:s['wall_each_s'] for s in spec['execution_contract_proposal']['scene_caps']}
    rows=[]
    for name in SCENE_ORDER:
        for variant in ('base','refined'):
            rows.append(dict(scene=name,variant=variant,wall_cap_s=limits[name],solver=numerical['base_solver'],
                tolerance=numerical[variant+'_tolerance'],output_interval_s=numerical[variant+'_output_interval'],
                requires_previous_pass=bool(rows),requires_base_csv=variant=='refined'))
    return dict(status='PLAN_ONLY_NO_EXECUTION_AUTHORITY',physical_sha256=PHYSICAL_SHA,scenes=rows,
        scene_count=len(rows),complete_set_wall_cap_s=sum(r['wall_cap_s'] for r in rows),
        failure_audit=0,failure_result='UNKNOWN_WITH_ORIGINAL_FAILED_CHECKS',vehicle_result='UNKNOWN',
        product_integration='PENDING_PROFILE_AND_WORKER_AND_FINAL_COMMIT_BINDING')
