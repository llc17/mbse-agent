"""Explicit source preparation, external confirmation and bounded round modes."""
import argparse
from week12.src.integration import dimension_execution as execution


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--initialize-receipt')
    mode.add_argument('--round', type=int)
    args = parser.parse_args()
    if args.prepare:
        packet = execution.prepare()
        print('SOURCE_ONLY_PREPARED_NOT_AUTHORIZED', packet['specification_digest'])
    elif args.initialize_receipt:
        execution.initialize(args.initialize_receipt)
        print('APPROVED_NOT_RUN')
    else:
        report = execution.run_round(args.round)
        print(report['status'], report.get('error', ''))
        if report['status'] == 'STOPPED': raise SystemExit(1)


if __name__ == '__main__': main()
