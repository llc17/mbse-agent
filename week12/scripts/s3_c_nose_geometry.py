"""Source-only preparation, external receipt initialization, finite guarded round."""
import argparse
import json
from week12.src.integration import nose_geometry_execution as execution


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare',action='store_true')
    action.add_argument('--initialize-receipt')
    action.add_argument('--round',type=int,choices=(1,2,3))
    args = parser.parse_args()
    if args.prepare:
        result = execution.prepare()
        print(json.dumps({'status':'SOURCE_ONLY_PREPARED','specification_digest':execution.SPEC,
                          'execution_manifest_digest':result['execution_manifest_digest']},ensure_ascii=False))
    elif args.initialize_receipt:
        execution.initialize(args.initialize_receipt)
        print(json.dumps({'status':'APPROVED_NOT_RUN','specification_digest':execution.SPEC}))
    else:
        result = execution.run_round(args.round)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        if result['status'] != 'PASS_DIMENSION_SMALL_SCENES_C_INCOMPLETE': raise SystemExit(1)


if __name__ == '__main__': main()
