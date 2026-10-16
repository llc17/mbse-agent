"""One-shot reproduction of byte-identical user models; no parameter overrides."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results/user-4w2n/original-20260922-01'
SOURCE = ROOT / 'models/user-4w2n/source-v1'
OMC = Path('D:/openmodelica/bin/omc.exe')
CASES = {
    'up': ('运动/6lun_up.mo', 'ZLTECH_Marathon_Final', 60, 600, 1e-4),
    'energy': ('能量/power_limit.mo', 'ZLTECH_48V_60Ah_Endurance', 60000, 6000, 1e-5),
    'down': ('运动/6lun_down.mo', 'ZLTECH_Descent_Turbo', 60, 3000, 1e-2),
    'full': ('运动/6lun_zhenggeliucheng.mo', 'ZLTECH_Full_Mission', 120, 2400, 1e-2),
}

def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def run(command, folder, label, timeout):
    env = os.environ.copy()
    env['PATH'] = str(OMC.parent) + os.pathsep + env.get('PATH', '')
    record = dict(command=command, cwd=str(folder), timeout_s=timeout, started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    write_json(folder / (label + '.json'), record)
    start = time.monotonic()
    with (folder / (label + '.stdout.log')).open('wb') as stdout, (folder / (label + '.stderr.log')).open('wb') as stderr:
        proc = subprocess.Popen(command, cwd=folder, env=env, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            proc.wait(timeout=timeout)
            record['status'] = 'process_exited'
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            record['status'] = 'timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'interrupted'
            cleanup = subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            record['cleanup_returncode'] = cleanup.returncode
            (folder / (label + '.cleanup.log')).write_bytes(cleanup.stdout + cleanup.stderr)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                record['cleanup_uncertain'] = True
        record.update(returncode=proc.poll(), duration_s=time.monotonic()-start)
    write_json(folder / (label + '.json'), record)
    return record

def prepare():
    for key, (relative, model, stop, intervals, tol) in CASES.items():
        folder = OUT / 'runs' / key
        folder.mkdir(parents=True, exist_ok=False)
        source = SOURCE / relative
        shutil.copy2(source, folder / 'original.mo')
        assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256((folder / 'original.mo').read_bytes()).digest()
        mos = f'''getVersion();
loadFile("original.mo");
getSimulationOptions({model});
buildModel({model},startTime=0,stopTime={stop},numberOfIntervals={intervals},tolerance={tol},fileNamePrefix="model",outputFormat="mat");
getErrorString();
'''
        (folder / 'build.mos').write_text(mos, encoding='utf-8')
        result = run([str(OMC), 'build.mos'], folder, 'build', 120)
        if result['returncode'] != 0 or not (folder / 'model.exe').is_file():
            print(key + ': build failed; batch stopped', flush=True)
            return
        xml = ET.parse(folder / 'model_init.xml').getroot()
        options = xml.find('DefaultExperiment').attrib
        write_json(folder / 'effective-settings.json', dict(model=model, source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), settings=options))
        print(key + ': build complete; simulation not started; ' + json.dumps(options), flush=True)

def simulate(key):
    protocol = json.loads((OUT / 'protocol.json').read_text(encoding='utf-8'))
    if protocol.get('execution_status') != 'ready':
        raise RuntimeError('Protocol is not ready; original solver and comparison scope are pending.')
    if protocol.get('batch_stopped'):
        raise RuntimeError('Batch stopped; no automatic retry or continuation.')
    folder = OUT / 'runs' / key
    claim = folder / 'simulation.claim.json'
    with claim.open('x', encoding='utf-8') as stream:
        json.dump(dict(attempt=1, retries_allowed=0), stream)
    expected = json.loads((folder / 'effective-settings.json').read_text(encoding='utf-8'))['source_sha256']
    if hashlib.sha256((folder / 'original.mo').read_bytes()).hexdigest() != expected:
        raise RuntimeError('Working source changed')
    command = [str(folder / 'model.exe'), '-r=result.mat']
    solver = protocol.get('runtime_solver_overrides', {}).get(key)
    if solver:
        command += ['-s=' + solver]
    result = run(command, folder, 'simulation', 300)
    if result['returncode'] != 0 or result['status'] != 'process_exited':
        protocol['batch_stopped'] = True
        protocol['stop_reason'] = key + ': runtime failure or timeout'
        write_json(OUT / 'protocol.json', protocol)
    print(json.dumps(result, ensure_ascii=False))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--case', choices=list(CASES))
    args = parser.parse_args()
    if args.prepare:
        prepare()
    elif args.case:
        simulate(args.case)
    else:
        parser.error('Choose --prepare or --case')
