"""Bounded subprocess entry for B result checking; called only after an approved run."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from week12.src.component_physics.contract import ROOT, canonical
from week12.src.component_physics.verify import verification_outputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('ident', choices=['B01', 'B02', 'B03', 'B04', 'B05', 'B06'])
    parser.add_argument('csv')
    parser.add_argument('--base')
    parser.add_argument('--output-directory', help='New evidence directory for approved reprocessing; never overwrite original results')
    args = parser.parse_args()
    path = Path(args.csv).resolve()
    if not path.is_relative_to(ROOT/'results'):
        raise ValueError('B output must stay inside V11 results')
    base = None
    if args.base:
        base = Path(args.base).resolve()
        if not base.is_relative_to(ROOT/'results'):
            raise ValueError('Baseline outside V11 results')
    output = Path(args.output_directory).resolve() if args.output_directory else path.parent
    if not output.is_relative_to(ROOT/'results'):
        raise ValueError('Output directory outside V11 results')
    if args.output_directory:
        output.mkdir(exist_ok=False)
    elif any((output/(name+'.json')).exists() for name in ('physical', 'base_physical', 'numerical')):
        raise FileExistsError('Prior verification evidence must not be overwritten')
    for name, result in verification_outputs(args.ident, path, base):
        with (output/(name+'.json')).open('xb') as handle:
            handle.write(canonical(result))
        if result['status'] != 'PASS':
            print(name.upper()+'_FAIL')
            return 1
    print('PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
