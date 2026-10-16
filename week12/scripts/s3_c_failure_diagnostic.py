"""Explicit preparation/receipt/round CLI; no default run and no new receipt."""
import argparse
from week12.src.integration import failure_diagnostic as diagnostic


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--initialize-receipt')
    mode.add_argument('--round', type=int)
    mode.add_argument('--compare')
    args = parser.parse_args()
    if args.prepare:
        result = diagnostic.prepare()
        print('SOURCE_ONLY_PREPARED_NOT_AUTHORIZED', result['specification_digest'])
    elif args.initialize_receipt:
        diagnostic.initialize(args.initialize_receipt)
        print('APPROVED_NOT_RUN')
    elif args.round is not None:
        result = diagnostic.run_round(args.round)
        print(result['status'], result.get('classification', ''), result.get('error', ''))
        if result['status'] == 'STOPPED': raise SystemExit(1)
    else:
        result = diagnostic.compare_folder(args.compare)
        print(result['classification'])
        if result['classification'] == 'INCOMPLETE': raise SystemExit(1)


if __name__ == '__main__': main()
