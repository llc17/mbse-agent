"""Explicit receipt initialization or one finite, approval-bound vehicle round."""
import argparse
from week12.src.integration.vehicle_amendment_execution import initialize, run_round


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--initialize-receipt', metavar='ABSOLUTE_EXISTING_RECEIPT')
    actions.add_argument('--round', type=int, choices=(1,2,3))
    args = parser.parse_args()
    if args.initialize_receipt:
        initialize(args.initialize_receipt)
        print('EXTERNAL_USER_RECEIPT_BOUND_NOT_YET_EXECUTED', flush=True)
    else:
        report = run_round(args.round)
        if not report['status'].startswith('PASS_'): raise SystemExit(1)


if __name__ == '__main__': main()
