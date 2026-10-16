"""Read existing MAT results only. Never builds or reruns a model."""
from pathlib import Path
from datetime import datetime, timezone
import json
import hashlib
import csv
import numpy as np
from scipy.io import loadmat
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results/user-4w2n/original-20260922-01'

def read_result(path):
    data = loadmat(path, chars_as_strings=False)
    # OpenModelica MAT v4 stores one variable per column of the name matrix.
    names = [''.join(data['name'][:, i]).replace('\x00', '').strip()
             for i in range(data['name'].shape[1])]
    result = {}
    for i, name in enumerate(names):
        block, index = (int(x) for x in data['dataInfo'][:2, i])
        values = data['data_2' if block == 0 else f'data_{block}']
        result[name] = values[abs(index)-1] * (1 if index > 0 else -1)
    assert np.all(np.diff(result['time']) >= 0)
    return result

def first_time(q, variable, threshold):
    indices = np.flatnonzero(q[variable] >= threshold)
    return float(q['time'][indices[0]]) if indices.size else None

def compare(name, actual, reference):
    target = reference['target']
    tolerance = reference.get('absolute_tolerance', abs(target)*reference.get('relative_tolerance', 0))
    delta = abs(actual-target) if actual is not None else None
    return dict(metric=name, actual=actual, target=target, absolute_tolerance=tolerance,
                absolute_difference=delta, status='PASS' if delta is not None and delta <= tolerance else 'FAIL')

def main():
    protocol = json.loads((OUT/'protocol.json').read_text(encoding='utf-8'))
    cases, traces, comparisons = {}, {}, {}
    for case in ('up', 'energy', 'down', 'full'):
        folder = OUT/'runs'/case
        q = traces[case] = read_result(folder/'result.mat')
        execution = json.loads((folder/'simulation.json').read_text(encoding='utf-8'))
        log = (folder/'simulation.stdout.log').read_text(encoding='utf-8')
        finite = all(np.isfinite(v).all() for v in q.values())
        termination = ('Simulation call terminate()' in log and abs(q['energy_remaining'][-1]-.1) < .001) if case == 'energy' else abs(q['time'][-1]-protocol['cases'][case]['stop_s']) < 1e-6
        record = dict(time_start_s=float(q['time'][0]), time_end_s=float(q['time'][-1]), finite=bool(finite),
                      runtime_status='PASS' if execution['returncode']==0 and 'The simulation finished successfully.' in log and finite and termination else 'FAIL',
                      reference_status='screenshot_features_only' if case in ('up','energy') else 'UNVERIFIED_no_original_result')
        columns = ['x','z','v','vz','psi','Fz_n','Fz_f','Fz_r','x_n','x_f','x_r','energy_remaining','current_power','SOC','pred_range_flat_km','pred_remaining_floors']
        for name in columns:
            if name in q:
                values = q[name]
                record[name] = dict(first=float(values[0]), last=float(values[-1]), min=float(values.min()), max=float(values.max()))
        if case == 'up':
            record['first_rear_exit_s'] = first_time(q, 'x_r', 7.2)
            measured = dict(final_z_m=float(q['z'][-1]), ascent_completion_s=record['first_rear_exit_s'], max_psi_rad=float(q['psi'].max()), max_Fz_n_N=float(q['Fz_n'].max()))
        elif case == 'energy':
            switched = np.flatnonzero(q['current_power'] > q['current_power'][0]+1)
            measured = dict(power_flat_W=float(q['current_power'][0]), power_climb_W=float(q['current_power'][-1]),
                            switch_s=float(q['time'][switched[0]]), depletion_s=float(q['time'][-1]),
                            pred_range_at_70s_km=float(np.interp(70, q['time'], q['pred_range_flat_km'])),
                            pred_floors_at_60s=float(np.interp(60, q['time'], q['pred_remaining_floors'])))
        else:
            endpoint = 28 if case == 'down' else 15.2
            record['exit_x_m'] = endpoint
            record['first_nose_exit_s'] = first_time(q, 'x_n', endpoint)
            record['first_rear_exit_s'] = first_time(q, 'x_r', endpoint)
            record['stopping_status'] = 'NOT_STOPPED_no_stop_controller'
        if case in ('up','energy'):
            comparisons[case] = [compare(name, value, protocol['comparison_proposal'][case][name]) for name,value in measured.items()]
        cases[case] = record
        selected = ['time'] + [name for name in columns if name in q]
        with (folder/'observed.csv').open('w',newline='',encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(selected)
            writer.writerows(zip(*(q[name] for name in selected)))
    manifest = json.loads((OUT/'source-manifest.json').read_text(encoding='utf-8'))
    integrity = []
    for item in manifest['files']:
        for label in ('source', 'snapshot'):
            path = Path(manifest[label])/item['path']
            integrity.append(dict(kind=label,path=str(path),matches=hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256']))
    for case in cases:
        folder = OUT/'runs'/case
        expected = json.loads((folder/'effective-settings.json').read_text(encoding='utf-8'))['source_sha256']
        integrity.append(dict(kind='working_model',path=str(folder/'original.mo'),matches=hashlib.sha256((folder/'original.mo').read_bytes()).hexdigest()==expected))
    summary = dict(analyzed_utc=datetime.now(timezone.utc).isoformat(), cases=cases, comparisons=comparisons,
                   source_integrity=integrity, product_acceptance='not_claimed', numerical_equivalence='unverified_no_reference_mat_csv',
                   procedure_deviation='All four executions were launched before numerical comparison. Exit codes were checked first, but this did not enforce the stated per-case comparison gate. Later comparison passed all frozen screenshot metrics. No reruns or parameter edits occurred.')
    (OUT/'observed-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    fig, axs = plt.subplots(2,2,figsize=(12,7),constrained_layout=True)
    q=traces['up']; axs[0,0].plot(q['time'],q['z']); axs[0,0].set(title='Ascent: 180 / 260 mm, 20 steps',ylabel='z (m)',xlabel='Time (s)')
    q=traces['down']; axs[0,1].plot(q['time'],q['z']); axs[0,1].set(title='Descent: 100 steps, unfinished at 60 s',ylabel='z (m)',xlabel='Time (s)')
    q=traces['full']; axs[1,0].plot(q['x'],q['z']); axs[1,0].axvline(15.2,color='gray',ls='--',label='Terrain end'); axs[1,0].legend(); axs[1,0].set(title='Full mission: terrain crossed, no stop',ylabel='z (m)',xlabel='x (m)')
    q=traces['energy']; axs[1,1].plot(q['time']/3600,q['current_power']); axs[1,1].set(title='Independent energy model: mixed mode',ylabel='Power (W)',xlabel='Time (h)')
    for ax in axs.flat: ax.grid(alpha=.25)
    fig.suptitle('Original user models / OpenModelica 1.25.7 / DASSL / one run each')
    fig.savefig(OUT/'observed-curves.png',dpi=160)
    plt.close(fig)
    print(json.dumps(dict(runtime={k:v['runtime_status'] for k,v in cases.items()},comparisons=comparisons,
                         full_rear_exit_s=cases['full']['first_rear_exit_s'],integrity_all_match=all(x['matches'] for x in integrity)),indent=2))

if __name__ == '__main__':
    main()
