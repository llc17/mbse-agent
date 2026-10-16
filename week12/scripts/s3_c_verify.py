"""Bounded runner subprocess for independent C result checks."""
import argparse
import json
from pathlib import Path

from week12.src.integration.vehicle_verify import verify, compare


def save(path,data):
    with Path(path).open('x',encoding='utf-8') as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.write('\n')


def main():
    parser=argparse.ArgumentParser()
    for name in ('proposal','contract','case','csv'): parser.add_argument('--'+name,required=True)
    parser.add_argument('--base')
    args=parser.parse_args()
    bundle=json.loads(Path(args.proposal).read_text(encoding='utf-8'))
    contract=json.loads(Path(args.contract).read_text(encoding='utf-8'))
    result=verify(bundle,contract,args.case,Path(args.csv))
    save(Path(args.csv).with_name('physical.json'),result)
    print('physical',result['status'],result.get('diagnostics'),flush=True)
    if result['status']!='PASS': raise SystemExit(1)
    if args.base:
        base_report=json.loads(Path(args.base).with_name('physical.json').read_text(encoding='utf-8'))
        result=compare(bundle,contract,args.case,Path(args.base),Path(args.csv),
                       base_report=base_report,refined_report=result)
        save(Path(args.csv).with_name('numerical.json'),result)
        print('numeric',result['status'],flush=True)
        if result['status']!='PASS': raise SystemExit(1)


if __name__=='__main__': main()
