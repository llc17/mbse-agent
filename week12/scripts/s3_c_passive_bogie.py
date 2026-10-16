"""Source preparation, externally approved rounds and bounded checker child."""
import argparse
import json
from pathlib import Path
from week12.src.integration import passive_bogie_execution as execution


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare',action='store_true')
    action.add_argument('--initialize-receipt')
    action.add_argument('--round',type=int,choices=(1,2,3))
    action.add_argument('--verify-scene')
    parser.add_argument('--bundle'); parser.add_argument('--contract')
    parser.add_argument('--case',choices=execution.CASE_ORDER); parser.add_argument('--base')
    args = parser.parse_args()
    if args.prepare:
        result = execution.prepare()
        print(json.dumps({'status':'SOURCE_ONLY_PREPARED','specification_digest':execution.SPEC,
                          'execution_manifest_digest':result['execution_manifest_digest']}))
    elif args.initialize_receipt:
        execution.initialize(args.initialize_receipt)
        print(json.dumps({'status':'APPROVED_NOT_RUN','specification_digest':execution.SPEC}))
    elif args.verify_scene:
        if not all((args.bundle,args.contract,args.case)): parser.error('checker requires --bundle --contract --case')
        from week12.src.integration.passive_bogie_verify import verify_scene
        result = verify_scene(Path(args.verify_scene),execution.read(args.bundle),execution.read(args.contract),
                              args.case,Path(args.base) if args.base else None)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        physical = execution.read(Path(args.verify_scene)/'physical.json')
        if physical['status'] != 'PASS': raise SystemExit(1)
        if args.base and execution.read(Path(args.verify_scene)/'numerical.json')['status'] != 'PASS': raise SystemExit(1)
    else:
        result = execution.run_round(args.round)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        if result['status'] != 'PASS_DIMENSION_SMALL_SCENES_C_INCOMPLETE': raise SystemExit(1)


if __name__ == '__main__': main()
