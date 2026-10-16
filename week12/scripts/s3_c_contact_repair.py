"""Explicit receipt and bounded-round interface; no inferred approvals."""
import argparse
from week12.src.integration.contact_repair_execution import initialize,run_round


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--initialize-receipt'); parser.add_argument('--round',type=int)
    args=parser.parse_args()
    if args.initialize_receipt: initialize(args.initialize_receipt)
    elif args.round:
        result=run_round(args.round); print(result)
        if not result['status'].startswith('PASS_'): raise SystemExit(1)
    else: parser.error('explicit receipt initialization or numbered round required')


if __name__=='__main__': main()
