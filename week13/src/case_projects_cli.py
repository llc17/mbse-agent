"""JSON-only local case import/catalog/replay/version comparison entry."""
import argparse
import json
from .case_projects import catalog_summaries,inspect_case,import_case,replay_case,compare_versions,discover_case


def main(argv=None):
    parser=argparse.ArgumentParser(description='V12 本地案例管理；不会批准预算或启动执行')
    parser.add_argument('action',choices=['list','inspect','import','replay','compare','discover'])
    parser.add_argument('--case-id');parser.add_argument('--path');parser.add_argument('--name');parser.add_argument('--model');parser.add_argument('--success-statement',default=None)
    parser.add_argument('--duplicate-mode',choices=['independent']);parser.add_argument('--left');parser.add_argument('--right')
    args=parser.parse_args(argv)
    try:
        if args.action=='list':result=dict(cases=catalog_summaries())
        elif args.action=='inspect':result=inspect_case(args.path,args.model)
        elif args.action=='import':result=import_case(args.path,args.name,args.model,args.success_statement or '',args.duplicate_mode)
        elif args.action=='replay':result=replay_case(args.case_id,args.name,args.model,args.success_statement)
        elif args.action=='compare':result=compare_versions(args.case_id,args.left,args.right)
        else:result=discover_case(args.case_id)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0
    except Exception as exc:
        print(json.dumps(dict(error=type(exc).__name__+': '+str(exc),state='STOP_AFFECTED_LOCAL_ACTION',product_http_added=0,execution_added=0),ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
